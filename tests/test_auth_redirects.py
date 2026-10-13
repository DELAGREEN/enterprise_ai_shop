import asyncio
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException
from starlette.requests import Request

from routers.auth import _safe_next
from routers.public import index


class AuthRedirectSafetyTests(unittest.TestCase):
    def test_safe_next_accepts_internal_paths(self):
        self.assertEqual(_safe_next('/dashboard'), '/dashboard')
        self.assertEqual(_safe_next('/chat/flow-123?next=/'), '/chat/flow-123?next=/')

    def test_safe_next_rejects_external_and_scheme_relative(self):
        for bad in [
            'https://evil.com',
            'http://evil.com/path',
            '//evil.com',
            'javascript:alert(1)',
        ]:
            with self.subTest(bad=bad):
                self.assertEqual(_safe_next(bad), '/')


class PublicRouteSafetyTests(unittest.TestCase):
    def test_public_index_requires_authenticated_user(self):
        request = Request({
            'type': 'http',
            'method': 'GET',
            'path': '/',
            'headers': [],
            'query_string': b'',
        })

        with self.assertRaises(HTTPException) as cm:
            asyncio.run(index(request, AsyncMock()))

        self.assertEqual(cm.exception.status_code, 401)


if __name__ == '__main__':
    unittest.main()
