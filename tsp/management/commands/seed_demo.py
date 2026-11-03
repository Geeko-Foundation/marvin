"""Fill the demo database matching MEETING-NOTES-2026-10-29.md."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone

from profile.groups import assign_user_to_group, ensure_groups
from profile.models import UserProfile
from tsp.models import Comment, Document, Event, Expense, TravelRequest
from tsp.services.state_machine import create_request_with_history, transition_request

PASSWORD = 'MarvinDemo!2026'
PDF = (
    b'%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n'
    b'2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n'
)


def make_user(
    username, first, last, groups, country='DE', city='Nuremberg',
    postal='90402', staff=False,
):
    user, _ = User.objects.get_or_create(
        username=username,
        defaults=dict(
            email=f'{username}@example.com',
            first_name=first,
            last_name=last,
            is_staff=staff,
            is_superuser=staff,
        ),
    )
    user.set_password(PASSWORD)
    user.first_name = first
    user.last_name = last
    user.is_staff = staff
    user.is_superuser = staff
    user.save()

    profile, _ = UserProfile.objects.get_or_create(
        user=user,
        defaults=dict(
            address_line='1 Demo Street',
            city=city,
            country=country,
            postal_code=postal,
            profile_completed_at=timezone.now(),
            banking_method='iban',
            banking_currency='EUR',
            wise_account_id=10000000 + user.pk,
            wise_recipient_verified_at=timezone.now(),
        ),
    )
    for i, group in enumerate(groups):
        assign_user_to_group(user, group, replace=(i == 0))
    return user


def make_request(user, event, title, description, expenses):
    tr = TravelRequest.objects.create(
        user=user,
        event=event,
        talk_title=title,
        description=description,
        created_by=user,
        changed_by=user,
    )
    create_request_with_history(tr, user)
    for category, text, amount, currency in expenses:
        Expense.objects.create(
            request=tr,
            category=category,
            description=text,
            estimated_amount=Decimal(amount),
            estimated_currency=currency,
        )
    return tr


def approve(tr, reviewer, notes='Approved as requested'):
    for e in tr.expenses.all():
        e.approved_amount, e.approved_currency = e.estimated_amount, e.estimated_currency
        e.save()
    transition_request(tr, 'approved', reviewer, notes)


def add_actuals(tr, ratio):
    for e in tr.expenses.all():
        e.actual_amount = (e.estimated_amount * Decimal(ratio)).quantize(Decimal('0.01'))
        e.actual_currency = e.estimated_currency
        e.save()


def add_documents(tr):
    for kind in ('receipt', 'invoice', 'signed_form'):
        doc = Document(
            request=tr,
            kind=kind,
            original_name=f'{kind}.pdf',
            mimetype='application/pdf',
            size=len(PDF),
            uploaded_by=tr.user,
        )
        doc.file.save(f'{kind}.pdf', ContentFile(PDF), save=False)
        doc.save()


class Command(BaseCommand):
    help = 'Populate the database with demo users, events, and requests.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Re-create demo data even if requests already exist.',
        )

    def handle(self, *args, **options):
        ensure_groups()

        if TravelRequest.objects.exists() and not options['force']:
            self.stdout.write(self.style.NOTICE(
                'Demo requests already exist. Use --force to recreate.'
            ))
            return

        requester = make_user('requester', 'Douglas', 'Demo', ['Requester'])
        maria = make_user('maria', 'Maria', 'Rossi', ['Requester'], 'IT', 'Bologna', '40121')
        arjun = make_user('arjun', 'Arjun', 'Mehta', ['Requester'], 'IN', 'Pune', '411001')
        committee = make_user('committee', 'Clara', 'Committee', ['TSP Committee'])
        finance = make_user('finance', 'Felix', 'Finance', ['Finance'])
        admin = make_user('admin', 'Ada', 'Admin', ['TSP Committee', 'Finance'], staff=True)

        oscon27, _ = Event.objects.get_or_create(
            name='openSUSE Conference 2027',
            defaults=dict(
                location='Prague, Czechia',
                start_date=dt.date(2027, 6, 14),
                end_date=dt.date(2027, 6, 16),
            ),
        )
        fosdem27, _ = Event.objects.get_or_create(
            name='FOSDEM 2027',
            defaults=dict(
                location='Brussels, Belgium',
                start_date=dt.date(2027, 1, 30),
                end_date=dt.date(2027, 1, 31),
            ),
        )
        lpc26, _ = Event.objects.get_or_create(
            name='Linux Plumbers Conference 2026',
            defaults=dict(
                location='Tokyo, Japan',
                start_date=dt.date(2026, 12, 9),
                end_date=dt.date(2026, 12, 11),
            ),
        )
        asia26, _ = Event.objects.get_or_create(
            name='openSUSE.Asia Summit 2026',
            defaults=dict(
                location='Bangkok, Thailand',
                start_date=dt.date(2026, 9, 18),
                end_date=dt.date(2026, 9, 19),
                accepting_requests=False,
            ),
        )
        Event.objects.get_or_create(
            name='FOSDEM 2026',
            defaults=dict(
                location='Brussels, Belgium',
                start_date=dt.date(2026, 2, 1),
                end_date=dt.date(2026, 2, 2),
                accepting_requests=False,
            ),
        )

        # 1. Waiting for review
        r1 = make_request(
            requester, oscon27, 'Packaging Rust crates for Tumbleweed',
            'Main-track talk on our new Rust packaging workflow.',
            [
                ('transportation', 'Train Nuremberg to Prague, return', '95.00', 'EUR'),
                ('lodging', 'Hotel, 3 nights', '360.00', 'EUR'),
            ],
        )
        r2 = make_request(
            maria, fosdem27, 'Distribution devroom: reproducible builds',
            'Devroom talk and booth shifts at the openSUSE stand.',
            [
                ('transportation', 'Flight Bologna to Brussels, return', '210.00', 'EUR'),
                ('lodging', 'Hostel, 2 nights', '140.00', 'EUR'),
            ],
        )
        r3 = make_request(
            arjun, lpc26, 'Kernel live patching on openSUSE',
            'Accepted talk in the live-patching microconference.',
            [
                ('transportation', 'Flight Mumbai to Tokyo, return', '780.00', 'USD'),
                ('lodging', 'Hotel, 4 nights', '520.00', 'USD'),
                ('visa', 'Japan visa fee', '25.00', 'USD'),
            ],
        )
        for tr in (r1, r2, r3):
            transition_request(tr, 'submitted', tr.user)

        Comment.objects.create(
            request=r1, user=committee, body='Can you share the talk acceptance email?',
            created_by=committee,
        )
        Comment.objects.create(
            request=r1, user=committee, internal=True, created_by=committee,
            body='Internal: budget for this event is fine, approve once confirmed.',
        )

        # 2. Draft
        make_request(
            requester, fosdem27, '', 'Planning to help at the booth.',
            [('transportation', 'Train to Brussels', '120.00', 'EUR')],
        )

        # 3. Approved
        r4 = make_request(
            requester, lpc26, 'Btrfs snapshots in practice', 'Lightning talk.',
            [
                ('transportation', 'Flight Frankfurt to Tokyo, return', '950.00', 'EUR'),
                ('lodging', 'Hotel, 3 nights', '420.00', 'EUR'),
            ],
        )
        transition_request(r4, 'submitted', requester)
        approve(r4, committee)

        # 4. Accepted
        r5 = make_request(
            requester, asia26, 'Community workshop: getting started with OBS',
            'Workshop host.',
            [
                ('transportation', 'Flight to Bangkok, return', '640.00', 'EUR'),
                ('lodging', 'Hotel, 2 nights', '180.00', 'EUR'),
            ],
        )
        transition_request(r5, 'submitted', requester)
        approve(r5, committee)
        transition_request(r5, 'accepted', requester)

        # 5. Submitted for payment with actual costs
        r6 = make_request(
            maria, asia26, 'Translating openSUSE docs', 'Talk and translation sprint.',
            [
                ('transportation', 'Flight Rome to Bangkok, return', '700.00', 'EUR'),
                ('lodging', 'Hotel, 2 nights', '200.00', 'EUR'),
            ],
        )
        transition_request(r6, 'submitted', maria)
        approve(r6, committee)
        transition_request(r6, 'accepted', maria)
        add_actuals(r6, '0.9')
        add_documents(r6)
        transition_request(r6, 'submitted_for_payment', maria)

        # 6. Paid
        r7 = make_request(
            arjun, asia26, 'Lightning talk: openSUSE in classrooms', 'Lightning talk.',
            [('transportation', 'Flight Pune to Bangkok, return', '310.00', 'USD')],
        )
        transition_request(r7, 'submitted', arjun)
        approve(r7, committee)
        transition_request(r7, 'accepted', arjun)
        add_actuals(r7, '1')
        add_documents(r7)
        transition_request(r7, 'submitted_for_payment', arjun)
        transition_request(r7, 'paid', finance, 'Paid by manual transfer')

        # 7. Rejected
        r8 = make_request(
            maria, lpc26, '', 'Would like to attend as a visitor.',
            [('transportation', 'Flight to Tokyo', '1100.00', 'EUR')],
        )
        transition_request(r8, 'submitted', maria)
        transition_request(r8, 'rejected', committee, 'We only fund speakers for this event.')

        self.stdout.write(self.style.SUCCESS(
            f'Demo data ready: {User.objects.count()} users, {TravelRequest.objects.count()} requests.'
        ))

