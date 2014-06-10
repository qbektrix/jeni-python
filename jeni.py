# jeni.py
# Copyright 2013-2014 Ron DuPlain <ron.duplain@gmail.com> (see AUTHORS file).
# Released under the BSD License (see LICENSE file).

"""jeni: dependency injection through annotations (dip)."""

__version__ = '0.3-dev'

import abc
import functools
import inspect
import re
import sys

import six


class UnsetError(LookupError):
    """Note is not able to be provided, as it is currently unset."""
    def __init__(self, *a, **kw):
        self.note = kw.pop('note', None)
        super(UnsetError, self).__init__(*a, **kw)


@six.add_metaclass(abc.ABCMeta)
class Provider(object):
    """Provide a single prepared dependency."""

    @abc.abstractmethod
    def get(self, name=None):
        """Implement in subclass."""

    def close(self):
        """By default, does nothing. Close objects as needed in subclass."""


class GeneratorProvider(Provider):
    """Manage generator lifecycle to implement Provider interface.

    `Injector` uses this class to support registering generators.
    When used directly, note that method `init` must be called before `get`::

        def generator(foo, bar):
            yield
            # continues when GeneratorProvider.close is called.
        provider = GeneratorProvider(generator)
        provider.init('foo', 'bar')
        provider.get()
    """

    def __init__(self, function, support_name=False):
        """Accept generator function & whether generator supports send."""
        if not inspect.isgeneratorfunction(function):
            msg = '{!r} is not a generator function'
            raise TypeError(msg.format(function))
        self.function = function
        self.support_name = support_name
        self.initialized = False

    def init(self, *a, **kw):
        """Call function to create generator, passing arguments provided."""
        self.generator = self.function(*a, **kw)
        try:
            self.init_value = next(self.generator)
        except StopIteration:
            msg = "generator didn't yield: function {!r}"
            raise RuntimeError(msg.format(self.function))
        else:
            self.initialized = True
            return self.init_value

    def get(self, name=None):
        """Get initial yield value, or result of send(name) if name given."""
        if not self.initialized:
            msg = '{!r} not initialized; call `init` before `get`.'
            raise RuntimeError(msg.format(self))
        if name is None:
            return self.init_value
        elif not self.support_name:
            msg = "generator does not support get-by-name: function {!r}"
            raise TypeError(msg.format(self.function))
        try:
            value = self.generator.send(name)
        except StopIteration:
            msg = "generator didn't yield: function {!r}"
            raise RuntimeError(msg.format(self.function))
        return value

    def close(self):
        """Close the generator."""
        if not self.initialized:
            raise RuntimeError('{!r} not initialized'.format(self))
        if self.support_name:
            self.generator.close()
        try:
            next(self.generator)
        except StopIteration:
            return
        else:
            msg = "generator didn't stop: function {!r}"
            raise RuntimeError(msg.format(self.function))


class Annotator(object):
    """Annotate callables. Intended to be stateless dict of function pointers.

    Annotations on callables are data for jeni's injection.
    Built as a class to embed annotation helpers and support customization.
    """

    # TODO: Support base-case to opt-in a function annotated in Python 3.
    # TODO: Support annotation to inject partial.

    def __call__(self, *notes, **keyword_notes):
        """Annotate a callable with a decorator to provide data for Injectors.

        Intended use::

            from jeni import annotate

            @annotate('foo', 'bar')
            def function(foo, bar):
                return

        An `Injector` would then need to register providers for 'foo' and 'bar'
        in order to apply this function; an injector with such providers can
        apply the annotated function without any further information::

            injector.apply(function)

        To get a partially applied function, to call later::

            fn = injector.partial(function)
            fn()

        Annotation does not alter the callable's default behavior.
        Call it normally::

            foo, bar = 'foo', 'bar'
            function(foo, bar)

        """
        def decorator(fn):
            self.set_annotations(fn, *notes, **keyword_notes)
            return fn
        return decorator

    # When getting or setting annotations, check callable for __func__. If
    # found, the callable is a method, and the __func__ as function object
    # should be used instead.

    def get_annotations(self, fn):
        """Get the annotations of a given callable."""
        if hasattr(fn, '__func__'):
            fn = fn.__func__
        if hasattr(fn, '__notes__'):
            return fn.__notes__
        raise AttributeError('{!r} does not have annotations'.format(fn))

    def set_annotations(self, fn, *notes, **keyword_notes):
        """Set the annotations on the given callable."""
        if hasattr(fn, '__func__'):
            fn = fn.__func__
        if hasattr(fn, '__notes__'):
            raise AttributeError('callable already has notes: {!r}'.format(fn))
        fn.__notes__ = (notes, keyword_notes)

    def has_annotations(self, fn):
        """True if callable is annotated, else False."""
        try:
            self.get_annotations(fn)
        except AttributeError:
            return False
        return True

annotate = Annotator()


class Injector(object):
    """Collects dependencies and reads annotations to inject them."""
    annotator_class = Annotator
    generator_provider = GeneratorProvider
    re_note = re.compile(r'^(.*?)(?::(.*))?$') # annotation is 'object:name'

    def __init__(self):
        """An Injector could take arguments to init, but this base does not.

        An Injector subclass inherits the provider registry of its base
        classes, but can override any provider by re-registering notes. When
        organizing a project, create an Injector subclass to serve as the
        object to register all providers. This allows for the project to have
        its own namespace of registered dependencies. This registry can be
        customized by further subclasses, either for injecting mocks in testing
        or providing alternative dependencies in a different runtime::

            from jeni import Injector as BaseInjector

            class Injector(BaseInjector):
                "Subclass provides namespace when registering providers."
        """
        annotator = self.annotator_class()
        self.get_annotations = annotator.get_annotations
        self.set_annotations = annotator.set_annotations
        self.has_annotations = annotator.has_annotations

        self.closed = False
        self.instances = {}
        self.values = {}

        self.get_order = []

        #: Statistics for resolved notes, note -> count.
        #: Records counts as soon as get is called, even if unset or error.
        self.stats = {}

    @classmethod
    def provider(cls, note, provider=None, name=False):
        """Register a provider, either a Provider class or a generator.

        Provider class::

            from jeni import Injector as BaseInjector
            from jeni import Provider

            class Injector(BaseInjector):
                pass

            @Injector.provider('hello')
            class HelloProvider(Provider):
                def get(self, name=None):
                    if name is None:
                        name = 'world'
                    return 'Hello, {}!'.format(name)

        Simple generator::

            @Injector.provider('answer')
            def answer():
                yield 42

        If a generator supports get with a name argument::

            @Injector.provider('spam', name=True)
            def spam():
                count_str = yield 'spam'
                while True:
                    count_str = yield 'spam' * int(count_str)

        Registration can be a decorator or a direct method call::

            Injector.provider('hello', HelloProvider)
        """
        def decorator(fn_or_class):
            if inspect.isgeneratorfunction(fn_or_class):
                fn = fn_or_class
                fn.support_name = name
                cls.register(note, fn)
            else:
                provider = fn_or_class
                if not hasattr(provider, 'get'):
                    msg = "{!r} does not meet provider interface with 'get'"
                    raise ValueError(msg.format(provider))
                cls.register(note, provider)
            return fn_or_class
        if provider is not None:
            decorator(provider)
        else:
            return decorator

    @classmethod
    def factory(cls, note, fn=None):
        """Register a function as a provider.

        Function (name support is optional)::

            from jeni import Injector as BaseInjector
            from jeni import Provider

            class Injector(BaseInjector):
                pass

            @Injector.factory('echo')
            def echo(name=None):
                return name

        Registration can be a decorator or a direct method call::

            Injector.factory('echo', echo)
        """
        if fn is not None:
            cls.register(note, fn)
        else:
            def decorator(f):
                cls.register(note, f)
                return f
            return decorator

    def apply(self, fn):
        """Fully apply annotated callable, returning callable's result."""
        args, kwargs = self.prepare_callable(fn)
        return fn(*args, **kwargs)

    def partial(self, fn):
        """Partially apply annotated callable, returning a partial function."""
        args, kwargs = self.prepare_callable(fn)
        return functools.partial(fn, *args, **kwargs)

    def get(self, note):
        """Resolve a single note into an object."""
        if self.closed:
            raise RuntimeError('{!r} already closed'.format(self))

        # Record request for note even if it fails to resolve.
        count = self.stats.get(note, 0)
        self.stats[note] = count + 1

        basenote, name = self.parse_note(note)
        if name is None and basenote in self.values:
            return self.values[basenote]
        try:
            provider_or_fn = self.lookup(basenote)
        except LookupError:
            msg = "Unable to resolve '{}'"
            raise LookupError(msg.format(note))
        return self.handle_provider(provider_or_fn, note)

    def close(self):
        """Close injector & injected Provider instances, including generators.

        Provider close methods should not intentionally raise errors.
        Specifically, if a dependency has transactions, the transaction should
        be committed or rolled back before close is called, and not left as an
        operation to be called during the close phase.

        Providers are closed in the reverse order in which they were opened,
        and each provider is only closed once. Providers are only closed if
        they have successfully provided a dependency via get.
        """
        if self.closed:
            raise RuntimeError('{!r} already closed'.format(self))
        for basenote in reversed(self.get_order):
            if basenote not in self.instances:
                # Provider is not an instance; no close implementation.
                continue
            self.instances[basenote].close()
        self.closed = True

    def prepare_callable(self, fn):
        """Prepare arguments required to apply function."""
        notes, keyword_notes = self.get_annotations(fn)
        args, kwargs = self.prepare_notes(*notes, **keyword_notes)
        return args, kwargs

    def prepare_notes(self, *notes, **keyword_notes):
        """Get injection values for all given notes."""
        args = tuple(self.get(note) for note in notes)
        kwargs = {}
        for arg in keyword_notes:
            # TODO: Maybe.
            note = keyword_notes[arg]
            try:
                kwargs[arg] = self.get(note)
            except UnsetError:
                continue
        return args, kwargs

    @classmethod
    def parse_note(cls, note):
        """Parse string annotation into object reference with optional name."""
        if isinstance(note, tuple):
            if len(note) != 2:
                raise ValueError('tuple annotations must be length 2')
            return note
        try:
            match = cls.re_note.match(note)
        except TypeError:
            # Note is not a string. Support any Python object as a note.
            return note, None
        return match.groups()

    def handle_provider(self, provider_or_fn, note):
        """Get value from provider as requested by note."""
        # Implementation in separate method to support accurate book-keeping.
        basenote, name = self.parse_note(note)
        result = self._handle_provider(provider_or_fn, note, basenote, name)
        if basenote not in self.get_order:
            self.get_order.append(basenote)
        return result

    def _handle_provider(self, provider_or_fn, note, basenote, name):
        if basenote in self.instances:
            provider_or_fn = self.instances[basenote]
        elif inspect.isclass(provider_or_fn):
            provider_or_fn = provider_or_fn()
            self.instances[basenote] = provider_or_fn
        elif inspect.isgeneratorfunction(provider_or_fn):
            provider_or_fn, value = self.init_generator(provider_or_fn)
            self.instances[basenote] = provider_or_fn
            self.values[basenote] = value
            if name is None:
                return value
        if hasattr(provider_or_fn, 'get'):
            fn = provider_or_fn.get
        else:
            fn = provider_or_fn
        if self.has_annotations(fn):
            fn = self.partial(fn)
        try:
            if name is None:
                value = fn()
                self.values[basenote] = value
                return value
            return fn(name=name)
        except UnsetError:
            # Use sys.exc_info to support both Python 2 and Python 3.
            exc_type, exc_value, tb = sys.exc_info()
            exc_value.note = note
            six.reraise(exc_type, exc_value, tb)

    @classmethod
    def register(cls, note, provider):
        """Implementation to register provider via `provider` & `factory`."""
        basenote, name = cls.parse_note(note)
        if 'provider_registry' not in vars(cls):
            cls.provider_registry = {}
        cls.provider_registry[basenote] = provider

    @classmethod
    def lookup(cls, basenote):
        """Look up note in registered annotations, walking class tree."""
        # Walk method resolution order, which includes current class.
        for c in cls.mro():
            if 'provider_registry' not in vars(c):
                # class is a mixin, super to base class, or never registered.
                continue
            if basenote in c.provider_registry:
                # note is in the registry.
                return c.provider_registry[basenote]
        raise LookupError(repr(basenote))

    def init_generator(self, fn):
        """Implementation to initialize generator providers."""
        provider = self.generator_provider(fn, support_name=fn.support_name)
        if self.has_annotations(provider.function):
            notes, keyword_notes = self.get_annotations(provider.function)
            args, kwargs = self.prepare_notes(*notes, **keyword_notes)
            value = provider.init(*args, **kwargs)
        else:
            value = provider.init()
        return provider, value

    def __enter__(self):
        """Support for context manager, returning self."""
        return self

    def enter(self):
        """Enter context-manager without with-block. See also: `exit`.

        Useful for before- and after-hooks which cannot use a with-block.
        """
        return self.__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        """Support for context manager, close on exit."""
        self.close()

    def exit(self):
        """Exit context-manager without with-block. See also: `enter`."""
        return self.__exit__(None, None, None)


class InjectorProxy(object):
    """Forwards getattr & getitem to enclosed injector.

    If an injector has 'hello' registered::

        from jeni import InjectorProxy
        deps = InjectorProxy(injector)
        deps.hello

    Get by name can use dict-style access::

        deps['hello:name']
    """

    def __init__(self, injector):
        if inspect.isclass(injector):
            msg = 'takes an instance not a class, {!r}'
            raise TypeError(msg.format(injector))
        self.injector = injector

    def __getattr__(self, name):
        return self.injector.get(name)

    def __getitem__(self, key):
        return self.injector.get(key)

    def __contains__(self, item):
        try:
            self.injector.get(item)
        except LookupError:
            return False
        return True


def class_in_progress(stack=None):
    """True if currently inside a class definition, else False."""
    if stack is None:
        stack = inspect.stack()
    for frame in stack:
        statement_list = frame[4]
        if statement_list is None:
            continue
        if statement_list[0].strip().startswith('class '):
            return True
    return False
