"""Small-page compression and authenticated, revalidated file delivery."""
import os

from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, Response


class FrontendCompression:
    """Compress UI/JSON only: PDF byte ranges and chat streams stay untouched."""

    def __init__(self, app):
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=1024, compresslevel=4)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        compress = path in {"/", "/api/files", "/api/history"} or path.startswith("/static/")
        await (self.compressed if compress else self.app)(scope, receive, send)


def private_file_response(request, path, *, media_type, headers):
    """Call only after authorization; a conditional request must also be authorized."""
    response = FileResponse(path, media_type=media_type, stat_result=os.stat(path),
                            headers={**headers, "Cache-Control": "private, no-cache",
                                     "Vary": "X-Session-ID"})
    matches = request.headers.get("if-none-match")
    if matches and any(tag.strip().removeprefix("W/") in {"*", response.headers["etag"]}
                       for tag in matches.split(",")):
        return Response(status_code=304, headers={key: response.headers[key] for key in
                        ("etag", "last-modified", "cache-control", "vary")})
    return response
