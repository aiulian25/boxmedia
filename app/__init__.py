"""BoxMedia.

The one place the running app names its own version. It is stamped into every backup
manifest and announced to the public APIs in the User-Agent, so it must match
pyproject.toml — it had drifted to 0.1.0 while the project shipped 1.2.1, which made
every manifest quietly wrong about the build that wrote it.

Read from here rather than from package metadata: the runtime image copies `app/` in
and never pip-installs the project as a distribution, so `importlib.metadata` would
raise there.
"""

__version__ = "1.4.0"
