import unittest

from calculator import divide


class CalculatorTests(unittest.TestCase):
    def test_divide(self) -> None:
        self.assertEqual(divide(8, 2), 4)

    def test_divide_with_decimals(self) -> None:
        self.assertAlmostEqual(divide(5, 2), 2.5)

    def test_divide_with_negative(self) -> None:
        self.assertEqual(divide(-6, 2), -3)
        self.assertEqual(divide(6, -2), -3)
        self.assertEqual(divide(-6, -2), 3)

    def test_divide_by_zero(self) -> None:
        with self.assertRaises(ZeroDivisionError):
            divide(1, 0)


if __name__ == "__main__":
    unittest.main()
