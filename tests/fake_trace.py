"""In-memory Hansken traces on the SDK interfaces, so plugin code can be unit tested without a Hansken server."""
import io

from hansken_extraction_plugin.api.extraction_trace import ExtractionTrace, ExtractionTraceBuilder


def flatten(properties, prefix=''):
    """{'file': {'name': 'x'}} -> {'file.name': 'x'}, the way plugins get() properties."""
    flat = {}
    for key, value in properties.items():
        if isinstance(value, dict):
            flat.update(flatten(value, f'{prefix}{key}.'))
        else:
            flat[prefix + key] = value
    return flat


class _Children:
    def _init_children(self):
        self.children = []

    def _new_child(self, name):
        child = FakeTraceBuilder(name)
        self.children.append(child)
        return child

    def child(self, name):
        matches = [child for child in self.children if child.get('name') == name]
        assert len(matches) == 1, f'expected one child {name!r}, got {[c.get("name") for c in self.children]}'
        return matches[0]

    def tree(self):
        """{name: subtree} of built children, a leaf is its raw data (or None)."""
        tree = {}
        for child in self.children:
            assert child.built, f'child {child.get("name")!r} was never built'
            tree[child.get('name')] = child.tree() if child.children else child.data.get('raw')
        return tree


class FakeTraceBuilder(_Children, ExtractionTraceBuilder):

    def __init__(self, name=None):
        self._init_children()
        self.properties = {'name': name} if name else {}
        self.data = {}
        self.built = False

    def get(self, key, default=None):
        return self.properties.get(key, default)

    def update(self, key_or_updates=None, value=None, data=None):
        assert not self.built, 'trace updated after build'
        if isinstance(key_or_updates, str):
            self.properties[key_or_updates] = value
        elif key_or_updates:
            self.properties.update(key_or_updates)
        self.data.update(data or {})
        return self

    def child_builder(self, name=None):
        assert self.built, 'the SDK requires a parent to be built before creating a child'
        return self._new_child(name)

    def build(self):
        assert not self.built, 'trace built twice'
        self.built = True
        return self.get('name')

    def add_tracelet(self, tracelet, value=None):
        raise NotImplementedError

    def add_transformation(self, data_type, transformation):
        raise NotImplementedError

    def open(self, *args, **kwargs):
        raise ValueError('the SDK does not support open() on child trace builders')


class FakeTrace(_Children, ExtractionTrace):
    """The trace offered to process(), with a raw data stream."""

    def __init__(self, properties, raw=b''):
        self._init_children()
        self.properties = flatten(properties)
        self.raw = raw

    def get(self, key, default=None):
        return self.properties.get(key, default)

    def update(self, key_or_updates=None, value=None, data=None):
        raise NotImplementedError

    def child_builder(self, name=None):
        return self._new_child(name)

    def open(self, data_type=None, offset=0, size=None, mode='rb', encoding='utf-8', buffer_size=None):
        assert mode == 'rb' and data_type in (None, 'raw')
        return io.BytesIO(self.raw)

    def add_tracelet(self, tracelet, value=None):
        raise NotImplementedError

    def add_transformation(self, data_type, transformation):
        raise NotImplementedError
