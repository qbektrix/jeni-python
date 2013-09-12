import unittest

import jeni


class TestProviderBasics(unittest.TestCase):
    def setUp(self):
        class Provider(jeni.BaseProvider):
            def get_x(self):
                return 6

            def get_y(self):
                return 7

        @Provider.annotate('x', 'y')
        def f(x, y, z=None):
            if z is not None:
                return x * y * z
            return x * y

        self.f = f
        self.provider = Provider()

    def test_call(self):
        self.assertEqual(42, self.f(6, 7))

    def test_apply(self):
        self.assertEqual(42, self.provider.apply(self.f))

    def test_partial(self):
        fn = self.provider.partial(self.f)
        self.assertEqual(42, fn())

    def test_partial_more(self):
        fn = self.provider.partial(self.f)
        self.assertEqual(4200, fn(100))
        self.assertEqual(4200, fn(z=100))


class TestProviderNotApplicable(unittest.TestCase):
    def setUp(self):
        self.provider = jeni.BaseProvider()

        def f(x, y):
            return x * y

        self.f = f

    def test_call(self):
        self.assertEqual(42, self.f(6, 7))

    def test_lookup_error_on_apply(self):
        self.assertRaises(LookupError, self.provider.apply, self.f)

    def test_lookup_error_on_partial(self):
        self.assertRaises(LookupError, self.provider.partial, self.f)


class TestProviderAccessByName(unittest.TestCase):
    def setUp(self):
        class Provider(jeni.BaseProvider):
            def get_thing(self, name=None):
                if name is not None:
                    return "thing with name '{}'".format(name)
                return 'thing without a name'

        @Provider.annotate('thing')
        def f(thing):
            return thing

        @Provider.annotate('thing:foo')
        def g(thing):
            return thing

        self.f = f
        self.g = g
        self.provider = Provider()

    def test_noname(self):
        self.assertEqual('thing without a name', self.provider.apply(self.f))

    def test_name(self):
        self.assertEqual("thing with name 'foo'", self.provider.apply(self.g))


class TestProviderArguments(unittest.TestCase):
    def setUp(self):
        class Provider(jeni.BaseProvider):
            def __init__(self, data):
                self.data = data

            def get_data(self, name=None):
                return self.data.get(name, jeni.UNSET)

        @Provider.annotate('data:x', 'data:y', fn='data:fn')
        def f(x, y, fn=None):
            if fn is None:
                fn = lambda *a: a
            return fn(x, y)

        self.f = f
        self.Provider = Provider

    def test_call(self):
        self.assertEqual((0, 1), self.f(0, 1))
        self.assertEqual(2, self.f(1, 1, fn=lambda *a: sum(a)))

    def test_unset_keyword(self):
        provider = self.Provider({'x': 0, 'y': 1})
        self.assertEqual((0, 1), provider.apply(self.f))

        another = self.Provider({'x': 1, 'y': 1, 'fn': lambda *a: sum(a)})
        self.assertEqual(2, another.apply(self.f))

    def test_unset_positional(self):
        provider = self.Provider({'y': 1})
        self.assertRaises(jeni.UnsetError, provider.apply, self.f)

        another = self.Provider({'y': 1, 'fn': lambda *a: sum(a)})
        self.assertRaises(jeni.UnsetError, another.apply, self.f)


class TestProviderCivics(unittest.TestCase):
    def setUp(self):
        class ProviderABC(jeni.BaseProvider):
            def get_a(self):
                return 2

            def get_b(self):
                return 4

            def get_c(self):
                return 8

            def get_fn(self):
                return lambda a, b, c: a * b * c

        self.ProviderABC = ProviderABC
        self.provider_abc = ProviderABC()

        class ProviderXYZ(jeni.BaseProvider):
            def get_x(self):
                return 98

            def get_y(self):
                return 99

            def get_z(self):
                return 100

            def get_fn(self):
                return lambda x, y, z: [x, y, z]

        self.ProviderXYZ = ProviderXYZ
        self.provider_xyz = ProviderXYZ()

        @ProviderABC.annotate('fn', 'a', 'b', 'c')
        @ProviderXYZ.annotate('fn', 'x', 'y', 'z')
        def g(fn, x, y, z):
            return fn(x, y, z)

        self.g = g

    def test_call(self):
        self.assertEqual(6, self.g(lambda *a: sum(a), 1, 2, 3))
        self.assertEqual(True, self.g(lambda *a: any(a), False, True, False))
        self.assertEqual(False, self.g(lambda *a: all(a), False, True, False))

    def test_apply(self):
        self.assertEqual(2 * 4 * 8, self.provider_abc.apply(self.g))
        self.assertEqual([98, 99, 100], self.provider_xyz.apply(self.g))

    def test_partial(self):
        abc = self.provider_abc.partial(self.g)
        self.assertEqual(2 * 4 * 8, abc())
        xyz = self.provider_xyz.partial(self.g)
        self.assertEqual([98, 99, 100], xyz())

    def test_extend(self):
        class Provider(jeni.BaseProvider):
            def get_fn(self):
                return lambda *a: sum(a)

        provider = Provider()
        provider.extend(self.provider_abc)
        self.assertRaises(LookupError, provider.apply, self.g)

        @Provider.annotate('fn', 'a', 'b', 'c')
        def do_g(*a):
            return self.g(*a)

        self.assertEqual(2 + 4 + 8, provider.apply(do_g))

    def test_extend_multiple(self):
        class Provider(jeni.BaseProvider):
            def get_fn(self):
                return lambda *a: sum(a)

        provider = Provider()
        provider.extend(self.provider_abc, self.provider_xyz)
        self.assertRaises(LookupError, provider.apply, self.g)

        @Provider.annotate('fn', 'a', 'b', 'z')
        def do_g(*a):
            return self.g(*a)

        self.assertEqual(2 + 4 + 100, provider.apply(do_g))

        base = jeni.BaseProvider()

        another = Provider()
        another.extend(base, self.provider_abc, self.provider_xyz)
        self.assertEqual(2 + 4 + 100, another.apply(do_g))

        yet_another = Provider()
        yet_another.extend(base)
        yet_another.extend(self.provider_abc)
        yet_another.extend(self.provider_xyz)
        self.assertEqual(2 + 4 + 100, yet_another.apply(do_g))

    def test_implement(self):
        class Provider(jeni.BaseProvider):
            def get_a(self):
                return 1010

            def get_b(self):
                return 2020

            def get_c(self):
                return 3030

            def get_fn(self):
                return lambda *a: sum(a)

        Provider.implement(self.ProviderABC)
        provider = Provider()
        self.assertEqual(1010 + 2020 + 3030, provider.apply(self.g))

    def test_implement_multiple(self):
        class BaseProvider(jeni.BaseProvider):
            def get_a(self):
                return 1010

            def get_b(self):
                return 2020

            def get_c(self):
                return 3030

            def get_x(self):
                return -3030

            def get_y(self):
                return -2020

            def get_z(self):
                return -1010

            def get_fn(self):
                return lambda *a: sum(a)

        class ProviderOne(BaseProvider):
            """Implements ABC then XYZ in one call."""

        ProviderOne.implement(self.ProviderABC, self.ProviderXYZ)
        provider_one = ProviderOne()
        self.assertEqual(1010 + 2020 + 3030, provider_one.apply(self.g))

        class ProviderTwo(BaseProvider):
            """Implements ABC then XYZ in separate calls."""

        ProviderTwo.implement(self.ProviderABC)
        ProviderTwo.implement(self.ProviderXYZ)
        provider_two = ProviderTwo()
        self.assertEqual(1010 + 2020 + 3030, provider_two.apply(self.g))

        class ProviderThree(BaseProvider):
            """Implements XYZ then ABC in one call."""

        ProviderThree.implement(self.ProviderXYZ, self.ProviderABC)
        provider_three = ProviderThree()
        self.assertEqual(-1010 + -2020 + -3030, provider_three.apply(self.g))

        class ProviderFour(BaseProvider):
            """Implements XYZ then ABC in separate call."""

        ProviderFour.implement(self.ProviderXYZ)
        ProviderFour.implement(self.ProviderABC)
        provider_four = ProviderFour()
        self.assertEqual(-1010 + -2020 + -3030, provider_four.apply(self.g))

        class ProviderFive(BaseProvider):
            """Implements Base then XYZ then ABC."""

        ProviderFive.implement(
            jeni.BaseProvider,
            self.ProviderXYZ,
            self.ProviderABC)
        provider_five = ProviderFive()
        self.assertEqual(-1010 + -2020 + -3030, provider_five.apply(self.g))


class TestSelfApply(unittest.TestCase):
    def setUp(self):
        class BaseProvider(jeni.BaseProvider):
            def get_x(self):
                return 6

            def get_y(self):
                return 7

        class Provider(BaseProvider):
            @BaseProvider.annotate('x', 'y')
            def calculate_z(self, x, y):
                return x * y

            def get_z(self):
                return self.apply(self.calculate_z)

        @Provider.annotate('z', 'y', 'x')
        def f(z, y, x):
            return z * y * x

        self.f = f
        self.provider = Provider()

    def test_call(self):
        self.assertEqual(42, self.provider.calculate_z(6, 7))

    def test_direct_apply(self):
        self.assertEqual(42, self.provider.apply(self.provider.calculate_z))

    def test_indirect_apply(self):
        self.assertEqual(42 * 7 * 6, self.provider.apply(self.f))


class TestConstructorAnnotation(unittest.TestCase):
    def setUp(self):
        class Provider(jeni.BaseProvider):
            def get_x(self):
                return 6

            def get_y(self):
                return 7

        @Provider.annotate('x', 'y')
        class Point(object):
            def __init__(self, x, y):
                self.x = x
                self.y = y

            def as_tuple(self):
                return self.x, self.y

        self.Point = Point
        self.provider = Provider()

    def test_call(self):
        point = self.Point(-1, 1)
        self.assertEqual((-1, 1), point.as_tuple())

    def test_apply(self):
        point = self.provider.apply(self.Point)
        self.assertIsInstance(point, self.Point)
        self.assertEqual((6, 7), point.as_tuple())

    def test_partial(self):
        create_point = self.provider.partial(self.Point)
        point = create_point()
        self.assertIsInstance(point, self.Point)
        self.assertEqual((6, 7), point.as_tuple())


if __name__ == '__main__':
    unittest.main()