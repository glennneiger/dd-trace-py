import logging
import math
import random
import sys
import time
import traceback

from .compat import StringIO, stringify, iteritems, numeric_types
from .ext import errors


log = logging.getLogger(__name__)


class Span(object):

    __slots__ = [
        # Public span attributes
        'service',
        'name',
        'resource',
        'span_id',
        'trace_id',
        'parent_id',
        'meta',
        'error',
        'metrics',
        'span_type',
        'start',
        'duration',
        # Sampler attributes
        'sampled',
        # Internal attributes
        '_tracer',
        '_finished',
        '_parent',
    ]

    def __init__(
        self,
        tracer,
        name,

        service=None,
        resource=None,
        span_type=None,
        trace_id=None,
        span_id=None,
        parent_id=None,
        start=None,
    ):
        """
        Create a new span. Call `finish` once the traced operation is over.

        :param Tracer tracer: the tracer that will submit this span when
                              finished.
        :param str name: the name of the traced operation.

        :param str service: the service name
        :param str resource: the resource name
        :param str span_type: the span type

        :param int trace_id: the id of this trace's root span.
        :param int parent_id: the id of this span's direct parent span.
        :param int span_id: the id of this span.

        :param int start: the start time of request as a unix epoch in seconds
        """
        # required span info
        self.name = name
        self.service = service
        self.resource = resource or name
        self.span_type = span_type

        # tags / metatdata
        self.meta = {}
        self.error = 0
        self.metrics = {}

        # timing
        self.start = start or time.time()
        self.duration = None

        # tracing
        self.trace_id = trace_id or _new_id()
        self.span_id = span_id or _new_id()
        self.parent_id = parent_id

        # sampling
        self.sampled = True

        self._tracer = tracer
        self._parent = None

        # state
        self._finished = False

    def finish(self, finish_time=None):
        """ Mark the end time of the span and submit it to the tracer.
            If the span has already been finished don't do anything

            :param int finish_time: the end time of the span in seconds.
                                    Defaults to now.
        """
        if self._finished:
            return
        self._finished = True

        if self.duration is None:
            ft = finish_time or time.time()
            # be defensive so we don't die if start isn't set
            self.duration = ft - (self.start or ft)

        if self._tracer:
            try:
                self._tracer.record(self)
            except Exception:
                log.exception("error recording finished trace")

    def set_tag(self, key, value):
        """ Set the given key / value tag pair on the span. Keys and values
            must be strings (or stringable). If a casting error occurs, it will
            be ignored.
        """
        try:
            self.meta[key] = stringify(value)
        except Exception:
            log.debug("error setting tag %s, ignoring it", key, exc_info=True)

    def get_tag(self, key):
        """ Return the given tag or None if it doesn't exist.
        """
        return self.meta.get(key, None)

    def set_tags(self, tags):
        """ Set a dictionary of tags on the given span. Keys and values
            must be strings (or stringable)
        """
        if tags:
            for k, v in iter(tags.items()):
                self.set_tag(k, v)

    def set_meta(self, k, v):
        self.set_tag(k, v)

    def set_metas(self, kvs):
        self.set_tags(kvs)

    def set_metric(self, key, value):
        # FIXME[matt] we could push this check to serialization time as well.

        # only permit types that are commonly serializable (don't use
        # isinstance so that we convert unserializable types like numpy
        # numbers)
        if type(value) not in numeric_types:
            try:
                value = float(value)
            except (ValueError, TypeError):
                log.debug("ignoring not number metric %s:%s", key, value)
                return

        # don't allow nan or inf
        if math.isnan(value) or math.isinf(value):
            log.debug("ignoring not real metric %s:%s", key, value)
            return

        self.metrics[key] = value

    def set_metrics(self, metrics):
        if metrics:
            for k, v in iteritems(metrics):
                self.set_metric(k, v)

    def get_metric(self, key):
        return self.metrics.get(key)

    def to_dict(self):
        d = {
            'trace_id' : self.trace_id,
            'parent_id' : self.parent_id,
            'span_id' : self.span_id,
            'service': self.service,
            'resource' : self.resource,
            'name' : self.name,
            'error': self.error,
        }

        if self.start:
            d['start'] = int(self.start * 1e9)  # ns

        if self.duration:
            d['duration'] = int(self.duration * 1e9)  # ns

        if self.meta:
            d['meta'] = self.meta

        if self.metrics:
            d['metrics'] = self.metrics

        if self.span_type:
            d['type'] = self.span_type

        return d

    def set_traceback(self):
        """ If the current stack has a traceback, tag the span with the
            relevant error info.

            >>> span.set_traceback()

            is equivalent to:

            >>> exc = sys.exc_info()
            >>> span.set_exc_info(*exc)
        """
        (exc_type, exc_val, exc_tb) = sys.exc_info()
        self.set_exc_info(exc_type, exc_val, exc_tb)

    def set_exc_info(self, exc_type, exc_val, exc_tb):
        """ Tag the span with an error tuple as from `sys.exc_info()`. """
        if not (exc_type and exc_val and exc_tb):
            return # nothing to do

        self.error = 1

        # get the traceback
        buff = StringIO()
        traceback.print_exception(exc_type, exc_val, exc_tb, file=buff, limit=20)
        tb = buff.getvalue()

        # readable version of type (e.g. exceptions.ZeroDivisionError)
        exc_type_str = "%s.%s" % (exc_type.__module__, exc_type.__name__)

        self.set_tag(errors.ERROR_MSG, exc_val)
        self.set_tag(errors.ERROR_TYPE, exc_type_str)
        self.set_tag(errors.ERROR_STACK, tb)

    def pprint(self):
        """ Return a human readable version of the span. """
        lines = [
            ('name', self.name),
            ("id", self.span_id),
            ("trace_id", self.trace_id),
            ("parent_id", self.parent_id),
            ("service", self.service),
            ("resource", self.resource),
            ('type', self.span_type),
            ("start", self.start),
            ("end", "" if not self.duration else self.start + self.duration),
            ("duration", "%fs" % (self.duration or 0)),
            ("error", self.error),
            ("tags", "")
        ]

        lines.extend((" ", "%s:%s" % kv) for kv in sorted(self.meta.items()))
        return "\n".join("%10s %s" % l for l in lines)

    def tracer(self):
        return self._tracer

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type:
                self.set_exc_info(exc_type, exc_val, exc_tb)
            self.finish()
        except Exception:
            log.exception("error closing trace")

    def __repr__(self):
        return "<Span(id=%s,trace_id=%s,parent_id=%s,name=%s)>" % (
            self.span_id,
            self.trace_id,
            self.parent_id,
            self.name,
        )

def _new_id():
    """Generate a random trace_id or span_id"""
    return random.getrandbits(64)
