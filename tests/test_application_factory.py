"""The application factory owns middleware/lifecycle, not shared domain state."""
from contextlib import asynccontextmanager
import unittest

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from hyadex.api.app import ApplicationBindings, create_app


class ApplicationFactoryTests(unittest.TestCase):
    def make_application(self, token):
        history = []
        lifecycle = []
        router = APIRouter()

        @router.post('/api/items')
        async def post_item(value: dict):
            history.append(value)
            return {'items': list(history)}

        @router.get('/api/items')
        async def get_items():
            return {'items': list(history)}

        async def authorize(request: Request, call_next):
            if request.headers.get('authorization') != f'Bearer {token}':
                return JSONResponse({'detail': 'unauthorized'}, status_code=401)
            return await call_next(request)

        @asynccontextmanager
        async def lifespan(app):
            lifecycle.append('start')
            yield
            lifecycle.append('stop')

        return create_app(ApplicationBindings(router, authorize, lifespan)), lifecycle

    def test_two_applications_keep_state_auth_and_lifecycles_separate(self):
        first, first_lifecycle = self.make_application('first')
        second, second_lifecycle = self.make_application('second')
        self.assertEqual(first_lifecycle + second_lifecycle, [])
        with TestClient(first) as a, TestClient(second) as b:
            self.assertEqual(a.get('/api/items').status_code, 401)
            self.assertEqual(b.get('/api/items', headers={'Authorization': 'Bearer first'}).status_code, 401)
            response = a.post('/api/items', json={'text': 'first'}, headers={'Authorization': 'Bearer first'})
            self.assertEqual(response.json(), {'items': [{'text': 'first'}]})
            self.assertEqual(b.get('/api/items', headers={'Authorization': 'Bearer second'}).json(), {'items': []})
            response = a.get('/api/items', headers={'Authorization': 'Bearer first', 'Origin': 'https://untrusted.invalid'})
            self.assertNotIn('access-control-allow-origin', response.headers)
            self.assertEqual(first_lifecycle, ['start'])
            self.assertEqual(second_lifecycle, ['start'])
        self.assertEqual(first_lifecycle, ['start', 'stop'])
        self.assertEqual(second_lifecycle, ['start', 'stop'])

    def test_included_router_lifespans_and_request_state_are_preserved(self):
        events = []

        @asynccontextmanager
        async def runtime_lifespan(app):
            events.append('runtime start')
            yield {'owner': 'runtime'}
            events.append('runtime stop')

        @asynccontextmanager
        async def child_lifespan(app):
            events.append('child start')
            yield {'child': 'ready', 'owner': 'child'}
            events.append('child stop')

        child = APIRouter(lifespan=child_lifespan)

        @child.get('/state')
        async def state(request: Request):
            return {'child': request.state.child, 'owner': request.state.owner}

        router = APIRouter()
        router.include_router(child)

        async def authorize(request, call_next):
            return await call_next(request)

        app = create_app(ApplicationBindings(router, authorize, runtime_lifespan))
        self.assertEqual(events, [])
        with TestClient(app) as client:
            self.assertEqual(client.get('/state').json(), {'child': 'ready', 'owner': 'runtime'})
            self.assertEqual(events, ['runtime start', 'child start'])
        self.assertEqual(events, ['runtime start', 'child start', 'child stop', 'runtime stop'])
