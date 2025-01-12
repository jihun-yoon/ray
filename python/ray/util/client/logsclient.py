"""This file implements a threaded stream controller to return logs back from
the ray clientserver.
"""
import sys
import logging
import queue
import threading
import grpc

from typing import TYPE_CHECKING

import ray.core.generated.ray_client_pb2 as ray_client_pb2
import ray.core.generated.ray_client_pb2_grpc as ray_client_pb2_grpc

if TYPE_CHECKING:
    from ray.util.client.worker import Worker

logger = logging.getLogger(__name__)
# TODO(barakmich): Running a logger in a logger causes loopback.
# The client logger need its own root -- possibly this one.
# For the moment, let's just not propogate beyond this point.
logger.propagate = False


class LogstreamClient:
    def __init__(self, client_worker: "Worker", metadata: list):
        """Initializes a thread-safe log stream over a Ray Client gRPC channel.

        Args:
            client_worker: The Ray Client worker that manages this client
            metadata: metadata to pass to gRPC requests
        """
        self.client_worker = client_worker
        self._metadata = metadata
        self.request_queue = queue.Queue()
        self.log_thread = self._start_logthread()
        self.log_thread.start()

    def _start_logthread(self) -> threading.Thread:
        return threading.Thread(target=self._log_main, args=(), daemon=True)

    def _log_main(self) -> None:
        stub = ray_client_pb2_grpc.RayletLogStreamerStub(
            self.client_worker.channel)
        log_stream = stub.Logstream(
            iter(self.request_queue.get, None), metadata=self._metadata)
        try:
            for record in log_stream:
                if record.level < 0:
                    self.stdstream(level=record.level, msg=record.msg)
                self.log(level=record.level, msg=record.msg)
        except grpc.RpcError as e:
            self._process_rpc_error(e)

    def _process_rpc_error(self, e: grpc.RpcError):
        """
        Processes RPC errors that occur while reading from data stream.
        """
        if e.code() == grpc.StatusCode.CANCELLED:
            # Graceful shutdown. We've cancelled our own connection.
            logger.info("Cancelling logs channel")
        elif e.code() in (grpc.StatusCode.UNAVAILABLE,
                          grpc.StatusCode.RESOURCE_EXHAUSTED):
            # TODO(barakmich): The server may have
            # dropped. In theory, we can retry, as per
            # https://grpc.github.io/grpc/core/md_doc_statuscodes.html but
            # in practice we may need to think about the correct semantics
            # here.
            logger.info("Server disconnected from logs channel")
        else:
            # Some other, unhandled, gRPC error
            logger.exception(
                f"Got Error from logger channel -- shutting down: {e}")

    def log(self, level: int, msg: str):
        """Log the message from the log stream.
        By default, calls logger.log but this can be overridden.

        Args:
            level: The loglevel of the received log message
            msg: The content of the message
        """
        logger.log(level=level, msg=msg)

    def stdstream(self, level: int, msg: str):
        """Log the stdout/stderr entry from the log stream.
        By default, calls print but this can be overridden.

        Args:
            level: The loglevel of the received log message
            msg: The content of the message
        """
        print_file = sys.stderr if level == -2 else sys.stdout
        print(msg, file=print_file, end="")

    def set_logstream_level(self, level: int):
        logger.setLevel(level)
        req = ray_client_pb2.LogSettingsRequest()
        req.enabled = True
        req.loglevel = level
        self.request_queue.put(req)

    def close(self) -> None:
        self.request_queue.put(None)
        if self.log_thread is not None:
            self.log_thread.join()

    def disable_logs(self) -> None:
        req = ray_client_pb2.LogSettingsRequest()
        req.enabled = False
        self.request_queue.put(req)
