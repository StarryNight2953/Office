# Packaged models

Place optional Blender-exported `.glb` assets in this directory. In the object
inspector, use `/models/example.glb`. Vite serves these files during development
and copies them into `dist/models` for the Python server's production preview.

Export in metres with glTF's Y-up convention and a sensible origin: furniture at
floor level, wall and ceiling equipment at its centre. The layout transform and
device binding remain independent of model geometry. Built-in procedural models
are used when no asset is specified or a model cannot be loaded.
