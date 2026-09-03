# Local (offline) transcription of voice/audio messages — pure logic only.
#
# Nothing in this package may import faster_whisper/ctranslate2 at module
# level: the backend is an optional component an install may simply not have,
# and the menu item that offers to install it has to be reachable on a machine
# where the import would fail. Every backend import lives inside the function
# that needs it.
