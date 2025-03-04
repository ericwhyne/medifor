from __future__ import print_function, division, unicode_literals, absolute_import

import contextlib
import json
import logging
import os
import select
import sys
import threading
import time
import traceback

from concurrent import futures

import medifor.v1.analytic_pb2 as analytic_pb2
import medifor.v1.analytic_pb2_grpc as analytic_pb2_grpc
import grpc
from grpc_health.v1 import health
from grpc_health.v1 import health_pb2
from grpc_health.v1 import health_pb2_grpc

from google.protobuf import json_format

def OptOutVideoLocalization(resp):
    """Opt out of all video localization types for a given video. Modifies resp."""
    resp.opt_out.extend([analytic_pb2.VIDEO_OPT_OUT_TEMPORAL,
                         analytic_pb2.VIDEO_OPT_OUT_SPATIAL])


def OptOutVideoAll(resp):
    """Opt out of all video processing for a given video. Modifies resp."""
    OptOutVideoLocalization(resp)
    resp.opt_out.append(analytic_pb2.VIDEO_OPT_OUT_DETECTION)


def OptOutVideoLocalization(resp):
    """Opt out of all video localization types for a given video. Modifies resp."""
    resp.opt_out.extend([analytic_pb2.VIDEO_OPT_OUT_TEMPORAL,
                         analytic_pb2.VIDEO_OPT_OUT_SPATIAL])


def OptOutVideoAll(resp):
    """Opt out of all video processing for a given video. Modifies resp."""
    OptOutVideoLocalization(resp)
    resp.opt_out.append(analytic_pb2.VIDEO_OPT_OUT_DETECTION)


class _AnalyticServicer(analytic_pb2_grpc.AnalyticServicer):
    """The class registered with gRPC, handles endpoints."""

    def __init__(self, svc):
        """Create a servicer using the given Service object as implementation."""
        self.svc = svc

    def DetectImageManipulation(self, req, ctx):
        return self.svc._CallEndpoint(self.svc.IMAGE_MANIPULATION, req, analytic_pb2.ImageManipulation(), ctx)

    def DetectVideoManipulation(self, req, ctx):
        return self.svc._CallEndpoint(self.svc.VIDEO_MANIPULATION, req, analytic_pb2.VideoManipulation(), ctx)

    def DetectImageSplice(self, req, ctx):
        return self.svc._CallEndpoint(self.svc.IMAGE_SPLICE, req, analytic_pb2.ImageSplice(), ctx)

    def DetectImageCameraMatch(self, req, ctx):
        return self.svc._CallEndpoint(self.svc.IMAGE_CAMERA_MATCH, req, analytic_pb2.ImageCameraMatch(), ctx)

class AnalyticService:
    """Actual implementation of the service, with function registration."""

    IMAGE_MANIPULATION = 'ImageManipulation'
    VIDEO_MANIPULATION = 'VideoManipulation'
    IMAGE_SPLICE = "ImageSplice"
    IMAGE_CAMERA_MATCH = "ImageCameraMatch"
    # Add to _ALLOWED_IMPLS if you add things here.

    _ALLOWED_IMPLS = frozenset([IMAGE_MANIPULATION, VIDEO_MANIPULATION, IMAGE_SPLICE, IMAGE_CAMERA_MATCH])

    def __init__(self):
        self._impls = {}
        self._health_servicer = health.HealthServicer()

    def Start(self, analytic_port=50051, max_workers=10, concurrency_safe=False):
        self.concurrency_safe = concurrency_safe
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers),
                             options=(('grpc.so_reuseport', 0),))
        analytic_pb2_grpc.add_AnalyticServicer_to_server(_AnalyticServicer(self), server)
        health_pb2_grpc.add_HealthServicer_to_server(self._health_servicer, server)
        if not server.add_insecure_port('[::]:{:d}'.format(analytic_port)):
            raise RuntimeError("can't bind to port {}: already in use".format(analytic_port))
        server.start()
        self._health_servicer.set('', health_pb2.HealthCheckResponse.SERVING)
        print("Analytic server started on port {} with PID {}".format(analytic_port, os.getpid()), file=sys.stderr)
        return server

    def Run(self, analytic_port=50051, max_workers=10, concurrency_safe=False):
        server = self.Start(analytic_port=analytic_port, max_workers=max_workers, concurrency_safe=concurrency_safe)

        try:
            while True:
                time.sleep(3600 * 24)
        except KeyboardInterrupt:
            server.stop(0)
            logging.info("Server stopped")
            return 0
        except Exception as e:
            server.stop(0)
            logging.error("Caught exception: %s", e)
            return -1

    def RegisterImageManipulation(self, f):
        return self._RegisterImpl(self.IMAGE_MANIPULATION, f)

    def RegisterVideoManipulation(self, f):
        return self._RegisterImpl(self.VIDEO_MANIPULATION, f)

    def RegisterImageSplice(self, f):
        return self._RegisterImpl(self.IMAGE_SPLICE, f)

    def RegisterImageCameraMatch(self, f):
        return self._RegisterImpl(self.IMAGE_CAMERA_MATCH, f)

    def _RegisterImpl(self, type_name, f):
        if type_name not in self._ALLOWED_IMPLS:
            raise ValueError("unknown implementation type {} specified".format(type_name))
        if type_name in self._impls:
            raise ValueError("implementation for {} already present".format(type_name))
        self._impls[type_name] = f
        return self

    def _CallEndpoint(self, ep_type, req, resp, ctx):
        """Implements calling endpoints and handling various exceptions that can come back.

        Args:
            ep_type: The name of the manipulation, e.g., "image". Should be in ALLOWED_IMPLS.
            req: The request proto to send.
            resp: The response proto to fill in.
            ctx: The context, used mainly for aborting with error codes.

        Returns:
            An appropriate response object for the endpoint type specified.
        """
        ep_func = self._impls.get(ep_type)
        if not ep_func:
            ctx.abort(grpc.StatusCode.UNIMPLEMENTED, "Endpoint {!r} not implemented".format(ep_type))

        try:
            ep_func(req, resp)
        except ValueError as e:
            logging.exception('invalid input')
            ctx.abort(grpc.StatusCode.INVALID_ARGUMENT, "Endpoint {!r} invalid input: {}".format(ep_type, e))
        except NotImplementedError as e:
            logging.warn('unimplemented endpoint {}'.format(ep_type))
            ctx.abort(grpc.StatusCode.UNIMPLEMENTED, "Endpoint {!r} not implemented: {}".format(ep_type, e))
        except Exception:
            logging.exception('unknown error')
            ctx.abort(grpc.StatusCode.UNKNOWN, "Error processing endpoint {!r}: {}".format(ep_type, traceback.format_exc()))
        return resp


class FIFOTimeoutError(IOError):
    def __init__(self, op, timeout):
        return super(FIFOTimeoutError, self).__init__("timed out with op {!r} after {} seconds".format(op, timeout))


class FIFOContextAbortedError(IOError):
    def __init__(self, code, details):
        self.code = code
        self.details = details
        super(FIFOContextAbortedError, self).__init__("Context aborted with code: {!s}.  Message: {!s}".format(code, details))


class FIFOContext:
    def abort(self, code, details):
        raise FIFOContextAbortedError(code, details)


class AnalyticServiceFIFO(AnalyticService):
    """Service implementation using a FIFO connection to be used when libraries preclude the use of grpc """

    DEFAULT_INFILE = "ANALYTIC_FIFO_IN"
    DEFAULT_OUTFILE = "ANALYTIC_FIFO_OUT"

    TYPES = {
        "imgmanip": (AnalyticService.IMAGE_MANIPULATION,
                     analytic_pb2.ImageManipulationRequest,
                     analytic_pb2.ImageManipulation),
        "vidmanip": (AnalyticService.VIDEO_MANIPULATION,
                     analytic_pb2.VideoManipulationRequest,
                     analytic_pb2.VideoManipulation),
        "imgsplice": (AnalyticService.IMAGE_SPLICE,
                      analytic_pb2.ImageSpliceRequest,
                      analytic_pb2.ImageSplice),
        "imgcammatch": (AnalyticService.IMAGE_CAMERA_MATCH,
                        analytic_pb2.ImageCameraMatchRequest,
                        analytic_pb2.ImageCameraMatch),
    }

    def __init__(self, infile=None, outfile=None):
        self.lock = threading.Lock()
        self.infile = infile or os.environ.get(self.DEFAULT_INFILE)
        self.outfile = outfile or os.environ.get(self.DEFAULT_OUTFILE)
        self.receiver = None
        self.sender = None
        super(AnalyticServiceFIFO, self).__init__()

    def _ensureOpen(self):
        # No lock here - called from main single-request-serving method.
        if not self.receiver:
            r = os.open(self.infile, os.O_RDONLY)
            self.receiver = os.fdopen(r, 'rt')
        if not self.sender:
            s = os.open(self.outfile, os.O_WRONLY)
            self.sender = os.fdopen(s, 'wt')

    def close(self):
        with self.lock:
            if self.receiver:
                self.receiver.close()
            if self.sender:
                self.sender.close()

    def send(self, data, timeout=0):
        self._ensureOpen()
        f = self.sender
        selArgs = [[], [f], [f]]
        if timeout:
            selArgs.append(timeout)

        if not any(select.select(*selArgs)):
            raise FIFOTimeoutError("write", timeout)

        f.write(data + '\n')
        f.flush()

    def receive(self, timeout=0):
        self._ensureOpen()
        f = self.receiver
        selArgs = [[f], [], [f]]
        if timeout:
            selArgs.append(timeout)

        if not any(select.select(*selArgs)):
            raise FIFOTimeoutError("read", timeout)
        return f.readline()

    def serveOnce(self):
        with self.lock:
            line = self.receive()
            msg = json.loads(line)
            if "type" not in msg:
                raise ValueError("Message had no 'type' field")
            callType, makeReq, makeResp = self.TYPES[msg["type"]]
            req, resp = makeReq(), makeResp()

            json_format.ParseDict(msg["value"], req)
            try:
                resp = self._CallEndpoint(callType, req, resp, FIFOContext())
                self.send(json.dumps({
                    "code": "OK",
                    "value": json_format.MessageToDict(resp),
                }))
            except FIFOContextAbortedError as e:
                self.send(json.dumps({
                    "code": str(e.code),
                    "value": e.details,
                }))

    def Run(self):
        """Run the service - listens to read FIFO and responds on write FIFO."""
        with contextlib.closing(self):
            while True:
                self.serveOnce()
