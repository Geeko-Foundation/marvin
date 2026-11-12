from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from marvin.validators import bank_value, multi_line, person_name, phone_number, postcode, single_line


class ValidatorTests(SimpleTestCase):
    """Real values in any language pass; markup, control codes and formulas do not."""

    def assert_ok(self, validator, *values):
        for value in values:
            with self.subTest(value=value):
                validator(value)

    def assert_refused(self, validator, *values):
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validator(value)

    def test_names_from_any_alphabet_are_accepted(self):
        self.assert_ok(person_name, 'José', "Mary-Jane O'Neil", 'J. R. R. Tolkien', 'Ng',
                       '李小龍', 'Ελένη', 'Анна-Мария', 'Nguyễn Văn An', 'de la Cruz')

    def test_names_refuse_markup_digits_formulas_and_control_codes(self):
        self.assert_refused(person_name, '<script>alert(1)</script>', 'Robert"; DROP',
                            '=HYPERLINK("http://x")', '+44 Bob', '@admin', 'Agent 007',
                            'Bob\x00', 'Bob\nSmith', '   ', "O''Neil")

    def test_a_single_line_refuses_line_breaks_tags_and_formula_starts(self):
        self.assert_ok(single_line, 'Flight to Prague, return', 'Hotel (3 nights) & breakfast')
        self.assert_refused(single_line, 'Flight\nto Prague', '<b>Flight</b>', '=1+1', '-1', 'x\x07')

    def test_long_text_keeps_line_breaks_but_refuses_control_codes(self):
        self.assert_ok(multi_line, 'Going to speak.\nSecond line.\tIndented.', 'Costs < 100 EUR')
        self.assert_refused(multi_line, 'hidden\x00byte', 'bell\x07')

    def test_phone_postcode_and_bank_values(self):
        self.assert_ok(phone_number, '+44 20 7946 0958', '(020) 7946-0958', '9876543210')
        self.assert_refused(phone_number, 'call me', '<123456>', '12')
        self.assert_ok(postcode, 'W6 9PL', '97201-1234', '01310-100', '411001')
        self.assert_refused(postcode, '<script>', 'W6\n9PL', '-123')
        self.assert_ok(bank_value, 'DE89 3704 0044 0532 0130 00', 'SBIN0001234', '123.456.789-09')
        self.assert_refused(bank_value, '<iban>', 'x' * 41, '=SUM(A1)')


class FormRuleTests(TestCase):
    """The rules reach the real forms, so a hostile value never gets stored."""

    def test_signup_refuses_a_name_with_markup(self):
        response = self.client.post(reverse('signup'), {
            'username': 'newperson', 'email': 'n@example.org',
            'first_name': '<script>alert(1)</script>', 'last_name': 'Smith',
            'password1': 'a-Long-pass-phrase-9', 'password2': 'a-Long-pass-phrase-9',
        })
        from django.contrib.auth.models import User
        self.assertFalse(User.objects.filter(username='newperson').exists())
        self.assertEqual(response.status_code, 200)

    def test_long_text_fields_have_a_limit(self):
        from tsp.forms import CommentForm
        form = CommentForm({'body': 'x' * 5001})
        self.assertFalse(form.is_valid())
        self.assertTrue(CommentForm({'body': 'x' * 5000}).is_valid())

    def test_an_international_phone_number_is_accepted_by_the_real_form(self):
        from profile.forms import ProfileForm
        form = ProfileForm(data={'phone': '+44 20 7946 0958'})
        form.is_valid()
        self.assertNotIn('phone', form.errors)
        form = ProfileForm(data={'phone': '=CMD()'})
        form.is_valid()
        self.assertIn('phone', form.errors)
