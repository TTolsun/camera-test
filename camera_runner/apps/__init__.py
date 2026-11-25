"""Camera app implementations available to the capture and device inspection commands."""

from . import galaxy_camera


DEFAULT_PACKAGE = galaxy_camera.profile.package
APPS = {DEFAULT_PACKAGE: galaxy_camera}


def app_option(package):
    """Keep suggested commands on the selected app while preserving the default command line."""
    return "" if package == DEFAULT_PACKAGE else f" --app {package}"


def classify(labels, app):
    """(recognized {key: label}, unrecognized [label]) for the labels observed in the mode strip and More."""
    recognized, unrecognized = {}, []
    for label in labels:
        key = app.mode_key(label)
        if key is None:
            unrecognized.append(label)
        else:
            recognized.setdefault(key, label)
    return recognized, unrecognized
