=====================
Prometheus Metrics
=====================

The featureflags server exposes Prometheus metrics that can be used to monitor the health and performance of the service.

HTTP Metrics
------------

.. describe:: http_request_total_per_project

   Total number of HTTP requests.

   :labels:
      - ``handler`` (string) - HTTP request handler (e.g. ``/load``, ``/sync``, ``/stream``)
      - ``project`` (string) - Project name

.. describe:: sse_streams

   Number of open ``/flags/stream`` connections on this instance.

.. describe:: project_version_listener_reconnects_total

   Number of times the ``LISTEN`` connection for project version changes had
   to be re-established.

Graph Metrics
-------------

.. describe:: action_time

   Action latency.

   :labels:
      - ``action`` (string) - Action name

.. describe:: action_errors

   Action errors count.

   :labels:
      - ``action`` (string) - Action name

.. describe:: graph_pull_time

   Graph pull time.

.. describe:: graph_pull_errors

   Graph pull errors count.