# jeni.py
# Copyright 2013-2014 Ron DuPlain <ron.duplain@gmail.com> (see AUTHORS file).
# Released under the BSD License (see LICENSE file).

"""``jeni`` injects annotated dependencies"""

__version__ = '0.3.6-dev'

import abc
import collections
import functools
import inspect
import re
import sys

import six


MAYBE = 'maybe'
PARTIAL = 'partial'
EAGER_PARTIAL = 'eager_partial'
WRAPPER_ASSIGNMENTS = functools.WRAPPER_ASSIGNMENTS + ('__notes__',)



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
        """Implement in subclass.

        Annotations in the form of ``'object:name'`` will pass the `name` value
        to the `get` method of the registered `Provider` (in this case, the
        provider registered with the `Injector` to provide `object`). This
        get-by-name pattern is useful for providers which have a dependency
        which supports lookups by key (e.g. HTTP headers or records in a
        key-value store).
        """

    def close(self):
        """By default, does nothing. Close objects as needed in subclass.

        Provider close methods should not intentionally raise errors.
        Specifically, if a dependency has transactions, the transaction should
        be committed or rolled back before close is called, and not left as an
        operation to be called during the close phase.

        Provider close methods must not take an argument; an injector cannot
        apply provided values on a close method since some providers may have
        already been closed. If an injected value is needed for the close
        method, annotate ``__init__`` and access the value via `self`.
        """


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


def see_doc(obj_with_doc):
    """Copy docstring from existing object to the decorated callable."""
    def decorator(fn):
        fn.__doc__ = obj_with_doc.__doc__
        return fn
    return decorator


class Annotator(object):
    """Class intent: serve as a stateless dict of function pointers.

    Annotate callables, settings data on callable objects themselves,
    providing hints for modes like maybe and partial.

    Annotations on callables are data for jeni's injection.
    Built as a class to embed annotation helpers and support customization.
    """

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

        On Python 2, use decorators to annotate.
        On Python 3, use either decorators or function annotations::

            from jeni import annotate

            @annotate
            def function(foo: 'foo', bar: 'bar'):
                return

        Note that when using Python function annotations, all injected values
        are provided as keyword arguments.

        Since function annotations could be interpreted differently by
        different packages, injectors do not use ``function.__annotations__``
        directly. Functions opt in by a simple ``@annotate``
        decoration. Functions with Python annotations which have not been
        decorated are assumed to not be decorated for injection.

        (For this reason, annotating a callable with a single note where the
        note is a callable is not supported.)

        Notes which are provided to `annotate` (above 'foo' and 'bar') can be
        any hashable object (i.e. object able to be used as a key in a dict)
        and is not limited to strings. If tuples are used as notes, they must
        be of length 2, and `('maybe', ...)` and `('partial', ...)` are
        reserved.
        """
        if not keyword_notes and len(notes) == 1 and is_callable(notes[0]):
            # Here @annotate is being used without arguments.
            fn = notes[0]
            if not getattr(fn, '__annotations__', None):
                msg = '{!r} does not have annotations'
                raise AttributeError(msg.format(fn))
            self.set_annotations(fn, **fn.__annotations__)
            return fn
        def decorator(__fn):
            self.set_annotations(__fn, *notes, **keyword_notes)
            return __fn
        return decorator

    # When getting or setting annotations, check callable for __func__. If
    # found, the callable is a method, and the __func__ as function object
    # should be used instead.

    @classmethod
    def get_annotations(cls, __fn):
        """Get the annotations of a given callable."""
        if hasattr(__fn, '__func__'):
            __fn = __fn.__func__
        if hasattr(__fn, '__notes__'):
            return __fn.__notes__
        raise AttributeError('{!r} does not have annotations'.format(__fn))

    @classmethod
    def set_annotations(cls, __fn, *notes, **keyword_notes):
        """Set the annotations on the given callable."""
        if hasattr(__fn, '__func__'):
            __fn = __fn.__func__
        if hasattr(__fn, '__notes__'):
            msg = 'callable already has notes: {!r}'
            raise AttributeError(msg.format(__fn))
        __fn.__notes__ = (notes, keyword_notes)

    @classmethod
    def has_annotations(cls, __fn):
        """True if callable is annotated, else False."""
        try:
            cls.get_annotations(__fn)
        except AttributeError:
            return False
        return True

    @staticmethod
    def wraps(__fn, **kw):
        """Like ``functools.wraps``, with support for annotations."""
        kw['assigned'] = kw.get('assigned', WRAPPER_ASSIGNMENTS)
        return functools.wraps(__fn, **kw)

    @staticmethod
    def maybe(note):
        """Wrap a keyword note to record that its resolution is optional.

        Normally all annotations require fulfilled dependencies, but if a
        keyword argument is annotated as `maybe`, then on apply, an injector
        does not attempt to pass dependencies which are unset or not provided::

            from jeni import annotate

            @annotate('foo', bar=annotate.maybe('bar'))
            def foobar(foo, bar=None):
                return
        """
        return (MAYBE, note)

    @staticmethod
    def partial(__fn, *a, **kw):
        """Wrap a note for injection of a partially applied function.

        This allows for annotated functions to be injected for composition::

            from jeni import annotate

            @annotate('foo', bar=annotate.maybe('bar'))
            def foobar(foo, bar=None):
                return

            @annotate('foo', annotate.partial(foobar))
            def bazquux(foo, fn):
                # fn: injector.partial(foobar)
                return

        Keyword arguments are treated as `maybe` when using partial, in order
        to allow partial application of only the notes which can be provided,
        where the caller could then apply arguments known to be unavailable in
        the injector. Note that with Python 3 function annotations, all
        annotations are injected as keyword arguments.

        Injections on the partial function are lazy and not applied until the
        injected partial function is called. See `eager_partial` to inject
        eagerly.
        """
        return (PARTIAL, (__fn, a, tuple(kw.items())))

    @staticmethod
    def eager_partial(__fn, *a, **kw):
        """Wrap a note for injection of an eagerly partially applied function.

        Use this instead of `partial` when eager injection is needed in place
        of lazy injection.
        """
        return (EAGER_PARTIAL, (__fn, a, tuple(kw.items())))


annotate = Annotator()
wraps = annotate.wraps
maybe = annotate.maybe
partial = annotate.partial
eager_partial = annotate.eager_partial


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
        self.annotator = self.annotator_class()

        self.closed = False
        self.instances = {}
        self.values = {}

        self.get_order = []

        #: Statistics for resolved notes, note -> count.
        #: Records counts as soon as get is called, even if unset or error.
        self.stats = collections.defaultdict(int)

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

    @classmethod
    def value(cls, note, scalar):
        """Register a single value to be provided.

        Supports base notes only, does not support get-by-name notes.
        """
        cls.factory(note, lambda: scalar)

    def apply(self, fn, *a, **kw):
        """Fully apply annotated callable, returning callable's result."""
        args, kwargs = self.prepare_callable(fn)
        args += a; kwargs.update(kw)
        return fn(*args, **kwargs)

    def partial(self, fn, *user_args, **user_kwargs):
        """Return function with closure to lazily inject annotated callable.

        Repeat calls to the resulting function will reuse injections from the
        first call.

        Positional arguments are provided in this order:

        1. positional arguments provided by injector
        2. positional arguments provided in `partial_fn = partial(fn, *args)`
        3. positional arguments provided in `partial_fn(*args)`

        Keyword arguments are resolved in this order (later override earlier):

        1. keyword arguments provided by injector
        2. keyword arguments provided in `partial_fn = partial(fn, **kwargs)`
        3. keyword arguments provided in `partial_fn(**kargs)`

        Note that Python function annotations (in Python 3) are injected as
        keyword arguments, as documented in `annotate`, which affects the
        argument order here.

        `annotate.partial` accepts arguments in same manner as this `partial`.
        """
        self.get_annotations(fn) # Assert has annotations.
        def lazy_injection_fn(*run_args, **run_kwargs):
            arg_pack = getattr(lazy_injection_fn, 'arg_pack', None)
            if arg_pack is not None:
                pack_args, pack_kwargs = arg_pack
            else:
                jeni_args, jeni_kwargs = self.prepare_callable(fn, partial=True)
                pack_args = jeni_args + user_args
                pack_kwargs = {}
                pack_kwargs.update(jeni_kwargs)
                pack_kwargs.update(user_kwargs)
                lazy_injection_fn.arg_pack = (pack_args, pack_kwargs)
            final_args = pack_args + run_args
            final_kwargs = {}
            final_kwargs.update(pack_kwargs)
            final_kwargs.update(run_kwargs)
            return fn(*final_args, **final_kwargs)
        return lazy_injection_fn

    def eager_partial(self, fn, *a, **kw):
        """Partially apply annotated callable, returning a partial function.

        By default, `partial` is lazy so that injections only happen when they
        are needed. Use `eager_partial` in place of `partial` when a guarantee
        of injection is needed at the time the partially applied function is
        created.

        `eager_partial` resolves arguments similarly to `partial` but relies on
        `functools.partial` for argument resolution when calling the final
        partial function.
        """
        args, kwargs = self.prepare_callable(fn, partial=True)
        args += a; kwargs.update(kw)
        return functools.partial(fn, *args, **kwargs)

    def apply_regardless(self, fn, *a, **kw):
        """Like `apply`, but applies if callable is not annotated."""
        if self.has_annotations(fn):
            return self.apply(fn, *a, **kw)
        return fn(*a, **kw)

    def partial_regardless(self, fn, *a, **kw):
        """Like `partial`, but applies if callable is not annotated."""
        if self.has_annotations(fn):
            return self.partial(fn, *a, **kw)
        else:
            return functools.partial(fn, *a, **kw)

    def eager_partial_regardless(self, fn, *a, **kw):
        """Like `eager_partial`, but applies if callable is not annotated."""
        if self.has_annotations(fn):
            return self.eager_partial(fn, *a, **kw)
        return functools.partial(fn, *a, **kw)

    def get(self, note):
        """Resolve a single note into an object."""
        if self.closed:
            raise RuntimeError('{!r} already closed'.format(self))

        # Record request for note even if it fails to resolve.
        self.stats[note] += 1

        # Handle injection of partially applied annotated functions.
        if isinstance(note, tuple) and len(note) == 2:
            if note[0] == PARTIAL:
                fn, a, kw_items = note[1]
                return self.partial(fn, *a, **dict(kw_items))
            elif note[0] == EAGER_PARTIAL:
                fn, a, kw_items = note[1]
                return self.eager_partial(fn, *a, **dict(kw_items))

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
            # Note: Unable to apply injector on close method.
            self.instances[basenote].close()
        self.closed = True

    def prepare_callable(self, fn, partial=False):
        """Prepare arguments required to apply function."""
        notes, keyword_notes = self.get_annotations(fn)
        return self.prepare_notes(*notes, __partial=partial, **keyword_notes)

    def prepare_notes(self, *notes, **keyword_notes):
        """Get injection values for all given notes."""
        __partial = keyword_notes.pop('__partial', False)
        args = tuple(self.get(note) for note in notes)
        kwargs = {}
        for arg in keyword_notes:
            note = keyword_notes[arg]
            if isinstance(note, tuple) and len(note) == 2 and note[0] == MAYBE:
                try:
                    kwargs[arg] = self.get(note[1])
                except LookupError:
                    continue
            elif __partial:
                try:
                    kwargs[arg] = self.get(note)
                except LookupError:
                    continue
            else:
                kwargs[arg] = self.get(note)
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
            # Inject class __init__, if annotated.
            cls = provider_or_fn
            if hasattr(cls, '__init__') and self.has_annotations(cls.__init__):
                args, kwargs = self.prepare_callable(cls.__init__)
                provider_or_fn = provider_or_fn(*args, **kwargs)
            else:
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
            exc_msg = str(exc_value)
            if exc_msg:
                msg = '{}: {!r}'.format(exc_msg, note)
            else:
                msg = repr(note)
            six.reraise(exc_type, exc_type(msg, note=note), tb)

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

    @see_doc(Annotator.get_annotations)
    def get_annotations(self, *a, **kw):
        return self.annotator.get_annotations(*a, **kw)

    @see_doc(Annotator.has_annotations)
    def has_annotations(self, *a, **kw):
        return self.annotator.has_annotations(*a, **kw)


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


def is_callable(obj):
    """True if object is callable, else False."""
    return hasattr(obj, '__call__')
