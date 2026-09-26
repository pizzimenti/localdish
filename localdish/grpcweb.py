"""gRPC-web client for the dish and router (see DESIGN.md "grpcweb.py"). Standard library only."""
from __future__ import annotations

# grpc status codes, by number (https://grpc.github.io/grpc/core/md_doc_statuscodes.html)
CODE_NAMES = {
    0: "OK", 1: "CANCELLED", 2: "UNKNOWN", 3: "INVALID_ARGUMENT", 4: "DEADLINE_EXCEEDED", 5: "NOT_FOUND",
    6: "ALREADY_EXISTS", 7: "PERMISSION_DENIED", 8: "RESOURCE_EXHAUSTED", 9: "FAILED_PRECONDITION", 10: "ABORTED",
    11: "OUT_OF_RANGE", 12: "UNIMPLEMENTED", 13: "INTERNAL", 14: "UNAVAILABLE", 15: "DATA_LOSS", 16: "UNAUTHENTICATED",
}


class GrpcError(Exception):
    """A device answered with a non-zero grpc-status."""

    def __init__(self, code: int, message: str = ""):
        self.code = code
        self.name = CODE_NAMES.get(code, str(code))
        self.message = message
        super().__init__(f"{self.name}: {message}" if message else self.name)
