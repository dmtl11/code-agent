import unittest

from calculator import divide


class CalculatorTests(unittest.TestCase):
    def test_divide(self) -> None:
        self.assertEqual(divide(8, 2), 4)


if __name__ == "__main__":
    unittest.main()
