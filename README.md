FeatureFlags client library for Python

See ``examples`` directory for complete examples for some major Web frameworks.

[PyPi](https://pypi.org/project/evo-featureflags-client)

Overview
--------

Client supports Python >=3.9.

Resilience options of the managers:

- ``stream=True`` (``HttpxManager`` only): changes are pushed by the server
  over SSE and synced right away; polling every ``refresh_interval`` seconds
  stays as a fallback.

Installation
------------

To install package:

- ``pdm add evo-featureflags-client``

To release package:

- ``lets release 0.4.0 --message="Added feature"``

Development
-----------

Install dependencies:

- ``pdm install -d``

Pre-commit

``./scripts/enable-hooks.sh``

``./scripts/disable-hooks.sh``

TODO:

- add docs, automate docs build
- add tests
- add `tracer` / `stats_collector` for http manager
