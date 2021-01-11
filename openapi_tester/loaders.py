import json
import logging
import os
from json import dumps, loads
from typing import Any, Callable, Optional, Union
from urllib.parse import ParseResult

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from openapi_spec_validator import openapi_v2_spec_validator, openapi_v3_spec_validator
from prance.util.resolver import RefResolver
from prance.util.url import ResolutionError

from openapi_tester.configuration import settings
from openapi_tester.exceptions import OpenAPISchemaError, UndocumentedSchemaSectionError
from openapi_tester.route import Route
from openapi_tester.utils import get_endpoint_paths, resolve_path, type_placeholder_value

logger = logging.getLogger('openapi_tester')


def handle_recursion_limit(schema: dict) -> Callable:
    """
    We are using a currying pattern to pass schema into the scope of the handler.
    """

    def handler(iteration: int, parse_result: ParseResult, recursions: tuple):
        try:
            fragment = parse_result.fragment
            keys = [key for key in fragment.split('/') if key]
            definition = schema
            for key in keys:
                definition = definition[key]
            return remove_recursive_ref(definition, fragment)
        except KeyError:
            return {}

    return handler


def remove_recursive_ref(schema: dict, fragment: str) -> dict:
    """
    Iterates over a dictionary to look for pesky recursive $refs using the fragment identifier.
    """
    for key, value in schema.items():
        if isinstance(value, dict):
            if '$ref' in value.keys() and fragment in value['$ref']:
                # TODO: use this value in the testing - to ignore some parts of the specs
                schema[key] = {'x-recursive-ref-replaced': True}
            else:
                schema[key] = remove_recursive_ref(schema[key], fragment)
    return schema


class BaseSchemaLoader:
    """
    Base class for OpenAPI schema loading classes.

    Contains a template of methods that are required from a loader class, and a range of helper methods for interacting
    with an OpenAPI schema.
    """

    base_path = '/'

    def __init__(self):
        super().__init__()
        self.schema: Optional[dict] = None
        self.original_schema: Optional[dict] = None

    def load_schema(self) -> dict:
        """
        Put logic required to load a schema and return it here.
        """
        raise NotImplementedError('The `load_schema` method has to be overwritten.')

    @staticmethod
    def index_schema(schema: dict, variable: str, error_addon: str = '') -> dict:
        """
        Indexes schema by string variable.

        :param schema: Schema to index
        :param variable: Variable to index by
        :param error_addon: Additional error info to be included in the printed error message
        :return: Indexed schema
        :raises: IndexError
        """
        try:
            logger.debug('Indexing schema by `%s`', variable)
            return schema[variable]
        except KeyError:
            raise UndocumentedSchemaSectionError(
                f'Failed indexing schema.\n\n'
                f'Error: Unsuccessfully tried to index the OpenAPI schema by `{variable}`. {error_addon}'
            )

    def get_schema(self) -> dict:
        """
        Returns OpenAPI schema.
        """
        if self.schema is None:
            self.set_schema(self.load_schema())
        return self.schema  # type: ignore

    def dereference_schema(self, schema: dict) -> dict:
        try:
            url = schema['basePath'] if 'basePath' in schema else self.base_path
            resolver = RefResolver(
                schema,
                recursion_limit_handler=handle_recursion_limit(schema),
                url=url,
            )
            resolver.resolve_references()
            return resolver.specs
        except ResolutionError as e:
            raise OpenAPISchemaError('infinite recursion error') from e

    @staticmethod
    def validate_schema(schema: dict):
        if 'openapi' in schema:
            validator = openapi_v3_spec_validator
        else:
            validator = openapi_v2_spec_validator
        validator.validate(schema)

    def set_schema(self, schema: dict) -> None:
        """
        Sets self.schema and self.original_schema.
        """
        dereferenced_schema = self.dereference_schema(schema)
        self.validate_schema(dereferenced_schema)
        self.original_schema = schema
        self.schema = self.dereference_schema(dereferenced_schema)

    def get_route(self, route: str) -> Route:
        """
        Returns the appropriate endpoint route.

        This method was primarily implemented because drf-yasg has its own route style, and so this method
        lets loader classes overwrite and add custom route conversion logic if required.
        """
        return Route(*resolve_path(route))

    def get_response_schema_section(self, route: str, method: str, status_code: Union[int, str], **kwargs) -> dict:
        """
        Indexes schema by url, HTTP method, and status code to get the schema section related to a specific response.

        :param route: Schema-compatible path
        :param method: HTTP request method
        :param status_code: HTTP response code
        :return Response schema
        """

        self.validate_method(method)
        self.validate_string(route, 'route')
        self.validate_status_code(status_code)
        route_object = self.get_route(route)
        schema = self.get_schema()

        # Index by paths
        paths_schema = BaseSchemaLoader.index_schema(schema=schema, variable='paths')

        # Index by route
        routes = ', '.join(list(paths_schema))
        route_error = ''
        if routes:
            pretty_routes = '\n\t• '.join(routes.split())

            if settings.parameterized_i18n_name:
                route_error += (
                    '\n\nDid you specify the correct i18n parameter name? '
                    f'Your project settings specify `{settings.parameterized_i18n_name}` '
                    f'as the name of your parameterized language, meaning a path like `/api/en/items` '
                    f'will be indexed as `/api/{{{settings.parameterized_i18n_name}}}/items`.'
                )
            route_error += f'\n\nFor debugging purposes, other valid routes include: \n\n\t• {pretty_routes}'

        if 'skip_validation_warning' in kwargs and kwargs['skip_validation_warning']:
            route_error += (
                f'\n\nTo skip validation for this route you can add `^{route}$` '
                f'to your VALIDATION_EXEMPT_URLS setting list in your OPENAPI_TESTER.MIDDLEWARE settings.'
            )

        error = None
        for _ in range(len(route_object.parameters) + 1):
            try:
                # This is an unfortunate piece of logic, where we're attempting to insert path parameters
                # one by one until the path works
                # if it never works, we finally raise an UndocumentedSchemaSectionError
                route_schema = BaseSchemaLoader.index_schema(
                    schema=paths_schema, variable=route_object.get_path(), error_addon=route_error
                )
                break
            except UndocumentedSchemaSectionError as e:
                error = e
            except IndexError:
                raise error  # type: ignore
        else:
            raise error  # type: ignore

        # Index by method
        joined_methods = ', '.join(method.upper() for method in route_schema.keys() if method.upper() != 'PARAMETERS')

        method_error = ''
        if joined_methods:
            method_error += f'\n\nAvailable methods include: {joined_methods}.'
        method_schema = BaseSchemaLoader.index_schema(
            schema=route_schema, variable=method.lower(), error_addon=method_error
        )

        # Index by responses
        responses_schema = BaseSchemaLoader.index_schema(schema=method_schema, variable='responses')

        # Index by status code
        responses = ', '.join(f'{code}' for code in responses_schema.keys())
        status_code_error = f' Is the `{status_code}` response documented?'
        if responses:
            status_code_error = f'\n\nDocumented responses include: {responses}. ' + status_code_error  # reverse add
        status_code_schema = BaseSchemaLoader.index_schema(
            schema=responses_schema, variable=str(status_code), error_addon=status_code_error
        )

        # Not sure about this logic - this is what my static schema looks like, but not the drf_yasg dynamic schema
        if 'content' in status_code_schema and 'application/json' in status_code_schema['content']:
            status_code_schema = status_code_schema['content']['application/json']

        return BaseSchemaLoader.index_schema(status_code_schema, 'schema')

    @staticmethod
    def validate_string(string: str, name: str) -> None:
        """
        Validates input as a string.
        """
        if not isinstance(string, str):
            raise ImproperlyConfigured(f'`{name}` is invalid.')

    @staticmethod
    def validate_method(method: str) -> str:
        """
        Validates a string as an HTTP method.

        :param method: HTTP method
        :raises: ImproperlyConfigured
        """
        methods = ['get', 'post', 'put', 'patch', 'delete', 'options', 'head']
        if not isinstance(method, str) or method.lower() not in methods:
            logger.error(
                'Method `%s` is invalid. Should be one of: %s.', method, ', '.join([i.upper() for i in methods])
            )
            raise ImproperlyConfigured(
                f'Method `{method}` is invalid. Should be one of: {", ".join([i.upper() for i in methods])}.'
            )
        return method

    @staticmethod
    def validate_status_code(status_code: Union[int, str]) -> None:
        """
        Validates a string or int as a valid HTTP response status code.

        :param status_code: the relevant HTTP response status code to check in the OpenAPI schema
        :raises: ImproperlyConfigured
        """
        try:
            status_code = int(status_code)
        except Exception:
            raise ImproperlyConfigured('`status_code` should be an integer.')
        if not 100 <= status_code <= 505:
            raise ImproperlyConfigured('`status_code` should be a valid HTTP response code.')

    def _iterate_schema_dict(self, schema_object: dict) -> dict:
        parsed_schema = {}
        if 'properties' in schema_object:
            properties = schema_object['properties']
        else:
            properties = {'': schema_object['additionalProperties']}
        for key, value in properties.items():
            if not isinstance(value, dict):
                raise ValueError()
            value_type = value['type']

            if 'example' in value:
                parsed_schema[key] = value['example']
            elif value_type == 'object':
                parsed_schema[key] = self._iterate_schema_dict(value)
            elif value_type == 'array':
                parsed_schema[key] = self._iterate_schema_list(value)  # type: ignore
            else:
                logger.warning('Item `%s` is missing an explicit example value', value)
                parsed_schema[key] = type_placeholder_value(value['type'])
        return parsed_schema

    def _iterate_schema_list(self, schema_array: dict) -> list:
        parsed_items = []
        raw_items = schema_array['items']
        items_type = raw_items['type']
        if 'example' in raw_items:
            parsed_items.append(raw_items['example'])
        elif items_type == 'object':
            parsed_items.append(self._iterate_schema_dict(raw_items))
        elif items_type == 'array':
            parsed_items.append(self._iterate_schema_list(raw_items))
        else:
            logger.warning('Item `%s` is missing an explicit example value', raw_items)
            parsed_items.append(type_placeholder_value(raw_items['type']))
        return parsed_items

    def create_dict_from_schema(self, schema: dict) -> Any:
        """
        Converts an OpenAPI schema representation of a dict to dict.
        """
        schema_type = schema['type']
        if 'example' in schema:
            return schema['example']
        elif schema_type == 'array':
            logger.debug('--> list')
            return self._iterate_schema_list(schema)
        elif schema_type == 'object':
            logger.debug('--> dict')
            return self._iterate_schema_dict(schema)
        else:
            logger.warning('Item `%s` is missing an explicit example value', schema)
            return type_placeholder_value(schema_type)


class DrfYasgSchemaLoader(BaseSchemaLoader):
    """
    Loads OpenAPI schema generated by drf_yasg.
    """

    def __init__(self) -> None:
        super().__init__()
        if 'drf_yasg' not in django_settings.INSTALLED_APPS:
            raise ImproperlyConfigured(
                'The package `drf_yasg` is missing from INSTALLED_APPS. Please add it to your '
                '`settings.py`, as it is required for this implementation'
            )
        from drf_yasg.generators import OpenAPISchemaGenerator
        from drf_yasg.openapi import Info

        logger.debug('Initialized drf-yasg loader schema')
        self.schema_generator = OpenAPISchemaGenerator(info=Info(title='', default_version=''))

    def load_schema(self) -> dict:
        """
        Loads generated schema from drf-yasg and returns it as a dict.
        """
        odict_schema = self.schema_generator.get_schema(None, True)
        schema = loads(dumps(odict_schema.as_odict()))
        logger.debug('Successfully loaded schema')
        return schema

    def get_path_prefix(self) -> str:
        """
        Returns the drf_yasg specified path prefix.

        Drf_yasg `cleans` schema paths by finding recurring path patterns,
        and cutting them out of the generated openapi schema.
        For example, `/api/v1/example` might then just become `/example`
        """

        return self.schema_generator.determine_path_prefix(get_endpoint_paths())

    def get_route(self, route: str) -> Route:
        """
        Returns a url that matches the urls found in a drf_yasg-generated schema.

        :param route: Django resolved route
        """

        deparameterized_path, resolved_path = resolve_path(route)
        path_prefix = self.get_path_prefix()  # typically might be 'api/' or 'api/v1/'
        if path_prefix == '/':
            path_prefix = ''
        logger.debug('Path prefix: %s', path_prefix)
        return Route(deparameterized_path=deparameterized_path[len(path_prefix) :], resolved_path=resolved_path)


class DrfSpectacularSchemaLoader(BaseSchemaLoader):
    """
    Loads OpenAPI schema generated by drf_spectacular.
    """

    def __init__(self) -> None:
        super().__init__()
        if 'drf_spectacular' not in django_settings.INSTALLED_APPS:
            raise ImproperlyConfigured(
                'The package `drf_spectacular` is missing from INSTALLED_APPS. Please add it to your '
                '`settings.py`, as it is required for this implementation'
            )
        from drf_spectacular.generators import SchemaGenerator

        self.schema_generator = SchemaGenerator()
        logger.debug('Initialized drf-spectacular loader schema')

    def load_schema(self) -> dict:
        """
        Loads generated schema from drf_spectacular and returns it as a dict.
        """
        return loads(dumps(self.schema_generator.get_schema(None, True)))

    def get_path_prefix(self) -> str:
        """
        Returns the drf_spectacular specified path prefix.
        """
        from drf_spectacular.settings import spectacular_settings

        return spectacular_settings.SCHEMA_PATH_PREFIX

    def get_route(self, route: str) -> Route:
        """
        Returns a url that matches the urls found in a drf_spectacular-generated schema.

        :param route: Django resolved route
        """
        from openapi_tester.utils import resolve_path

        deparameterized_path, resolved_path = resolve_path(route)
        path_prefix = self.get_path_prefix()  # typically might be 'api/' or 'api/v1/'
        if path_prefix == '/':
            path_prefix = ''
        logger.debug('Path prefix: %s', path_prefix)
        return Route(deparameterized_path=deparameterized_path[len(path_prefix) :], resolved_path=resolved_path)


class StaticSchemaLoader(BaseSchemaLoader):
    """
    Loads OpenAPI schema from a static file.
    """

    is_static_loader = True

    def __init__(self):
        super().__init__()
        self.path: str = ''
        logger.debug('Initialized static loader schema')

    def set_path(self, path: str) -> None:
        """
        Sets value for self.path
        """
        self.path = path

    def load_schema(self) -> dict:
        """
        Loads a static OpenAPI schema from file, and parses it to a python dict.

        :return: Schema contents as a dict
        :raises: ImproperlyConfigured
        """
        if not os.path.isfile(self.path):
            logger.error('Path `%s` does not resolve as a valid file.', self.path)
            raise ImproperlyConfigured(
                f'The path `{self.path}` does not point to a valid file. Make sure to point to the specification file.'
            )
        try:
            logger.debug('Fetching static schema from %s', self.path)
            with open(self.path) as f:
                content = f.read()
        except Exception as e:
            logger.exception('Exception raised when fetching OpenAPI schema from %s. Error: %s', self.path, e)
            raise ImproperlyConfigured(
                f'Unable to read the schema file. Please make sure the path setting is correct.\n\nError: {e}'
            )
        self.base_path = str(os.path.abspath(os.path.dirname(os.path.abspath(self.path))))
        if '.json' in self.path:
            schema = json.loads(content)
            logger.debug('Successfully loaded schema')
            return schema
        elif '.yaml' in self.path or '.yml' in self.path:
            import yaml

            schema = yaml.load(content, Loader=yaml.FullLoader)
            logger.debug('Successfully loaded schema')
            return schema
        else:
            raise ImproperlyConfigured('The specified file path does not seem to point to a JSON or YAML file.')