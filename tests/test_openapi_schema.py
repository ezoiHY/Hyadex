import asyncio
import unittest

from tests import hyadex_server
HTTP_METHODS = {
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
}

class OpenAPIOperationIdTests(unittest.TestCase):
    def schema(self) -> dict:
        previous = hyadex_server.app.openapi_schema
        try:
            hyadex_server.app.openapi_schema = None
            return hyadex_server.app.openapi()
        finally:
            hyadex_server.app.openapi_schema = previous

    def router_response(self, method: str, path: str) -> tuple[int, dict[str, str]]:
        async def dispatch() -> tuple[int, dict[str, str]]:
            messages: list[dict] = []

            async def receive() -> dict:
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message: dict) -> None:
                messages.append(message)

            await hyadex_server.app.router(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.3"},
                    "http_version": "1.1",
                    "method": method,
                    "scheme": "http",
                    "path": path,
                    "raw_path": path.encode("ascii"),
                    "query_string": b"",
                    "headers": [],
                    "client": ("127.0.0.1", 12345),
                    "server": ("127.0.0.1", 7870),
                    "root_path": "",
                },
                receive,
                send,
            )
            started = next(
                message
                for message in messages
                if message.get("type") == "http.response.start"
            )
            headers = {
                bytes(name).decode("latin-1").lower(): bytes(value).decode("latin-1")
                for name, value in started.get("headers", [])
            }
            return int(started["status"]), headers

        return asyncio.run(dispatch())

    def test_operation_ids_are_globally_unique_and_method_specific(self) -> None:
        schema = self.schema()
        uses: dict[str, list[tuple[str, str]]] = {}
        for path, path_item in schema["paths"].items():
            for method, operation in path_item.items():
                if method not in HTTP_METHODS:
                    continue
                operation_id = operation.get("operationId")
                self.assertIsInstance(operation_id, str)
                self.assertTrue(operation_id)
                uses.setdefault(operation_id, []).append((method, path))

        duplicates = {
            operation_id: locations
            for operation_id, locations in uses.items()
            if len(locations) > 1
        }
        self.assertEqual(duplicates, {})


if __name__ == "__main__":
    unittest.main()
