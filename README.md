# Marvin — Travel Support Program (TSP)

Marvin is a Django web application for managing travel support requests: speakers apply for event reimbursement, committee members review and approve requests, and finance staff process payments. It includes a requestor signup wizard, Wise sandbox integration for banking verification, a pluggable payments module, and optional browser push notifications.

## Tech stack

- **Python 3.13+** (tested with 3.13)
- **Django 6.x**
- **SQLite** (default, development)
- **django-tables2** — searchable data tables
- **django-webpush** — browser push notifications (opt-in)
- **DaisyUI / Tailwind** — UI styling (via templates)
- **Wise API** — sandbox recipient verification and payment initiation

## Project structure

```
marvin/           Django project settings and shared utilities
  audit.py        Abstract AuditModel (created_at, updated_at, created_by, changed_by)
profile/          User profiles, groups/permissions, Wise onboarding, push notifications
tsp/              Travel requests, events, expenses, documents, reports, search
payments/         Payment gateway plugin (Wise API, Wise CSV, manual transfer)
templates/        HTML templates
static/           CSS, images
uploads/          Uploaded receipt/invoice files (created at runtime)
logs/             Application logs (e.g. Wise API)
```

### Django apps

| App | Purpose |
|-----|---------|
| `profile` | `UserProfile`, role groups/permissions, signup/onboarding, Wise recipient services, push notification preferences |
| `tsp` | Events, travel requests, expenses, documents, state machine, dashboards, search |
| `payments` | Gateway-agnostic payment records and finance processing UI |
| `webpush` | Third-party app — browser subscription storage and service worker (via django-webpush) |

All domain models in `profile`, `tsp`, and `payments` inherit from `AuditModel`, which adds automatic timestamp and user tracking on every save:

- `created_at` — set when the record is first saved
- `updated_at` — updated on every save
- `created_by` / `changed_by` — populated from the logged-in user via `AuditUserMiddleware`

These fields are defined once in `marvin/audit.py`; concrete models inherit them without re-declaring them in each `models.py`.

## Prerequisites

- Python 3.11 or newer
- `pip` and `venv`

## Quick start

### 1. Clone and create a virtual environment

```bash
cd marvin
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run migrations

```bash
python manage.py migrate
```

### 4. Load demo data

```bash
python manage.py seed_data
python manage.py set_demo_passwords
```

`seed_data` loads users, groups, profiles, and events from `tsp/fixtures/initial.json`. It only runs on an empty database. `set_demo_passwords` sets every user's password to **`demo`**.

Alternatively, create a superuser manually:

```bash
python manage.py createsuperuser
```

### 5. Start the development server

```bash
python manage.py runserver
```

Open [http://127.0.0.1:8000/login/](http://127.0.0.1:8000/login/) in your browser.

## Demo accounts

After seeding, all users share the password **`demo`**. Sign in with username and password at `/login/`.

| Username | Role | Notes |
|----------|------|-------|
| `admin` | Staff / superuser | Full Django admin access |
| `requester` | Requester | Can submit travel requests |
| `finance` | Finance | Can process reimbursements |
| `superuser` | Staff superuser | Can skip Wise banking during onboarding |

Additional seeded users (`wizard1`, `ibanonly`, etc.) are available for testing edge cases.

## URLs

| URL | Description |
|-----|-------------|
| `/login/` | Application login (username + password) |
| `/signup/` | New requestor registration wizard |
| `/` | Role-based dashboard |
| `/profile/` | Personal profile, banking metadata, push notification settings |
| `/events/` | Browse and manage events |
| `/search/` | Search events (all users); search users (staff only) |
| `/requests/new/` | Create a travel request |
| `/reports/` | TSP reports (committee/finance) |
| `/payments/` | Finance payment queue |
| `/webpush/` | Push subscription endpoints and service worker |
| `/admin/` | Django admin (separate login at `/admin/login/`) |

## User roles

Roles are implemented as Django auth **groups** with custom permissions on `profile.UserProfile`:

- **Requester** — submit travel requests, complete profile and Wise onboarding
- **TSP Committee** — review, approve, reject, and manage events
- **Finance** — view reports and process reimbursements

Groups are created automatically after migrations via `profile.groups.ensure_groups()`.

## Wise integration (optional)

Wise runs in **sandbox mode** by default. Banking account numbers are sent to Wise only during verification — they are not stored in Marvin.

Staff and superusers may **skip Wise banking** during signup and complete it later from their profile.

Environment variables (all optional):

```bash
export WISE_API_TOKEN=your-sandbox-token
export WISE_API_PROFILE_ID=          # auto-resolved when empty
export WISE_API_SANDBOX=true
export WISE_SOURCE_CURRENCY=GBP
```

Wise API activity is logged to the console and `logs/wise.log`.

## Push notifications

Marvin uses [django-webpush](https://github.com/safwanrahman/django-webpush) for browser push notifications. Users opt in from **Profile → Push Notifications**.

1. Check **Enable push notifications** and save.
2. Click **Subscribe to Push Messaging** and allow browser permission.

Notifications are only sent to users who have opted in and subscribed a browser. Disabling push in the profile removes stored subscriptions.

Dev VAPID keys are included in settings. For production, generate your own and set:

```bash
export VAPID_PUBLIC_KEY=...
export VAPID_PRIVATE_KEY=...
export VAPID_ADMIN_EMAIL=admin@yourdomain.com
export WEBPUSH_APP_BASE_URL=https://yourdomain.com
```

### Events that send push notifications

| Event | Who is notified |
|-------|-----------------|
| Request submitted | TSP Committee |
| Request approved | Requester |
| Request rejected | Requester |
| Request accepted | Requester (reminder to upload documents) |
| Submitted for payment | Finance |
| Payment processed (paid) | Requester |
| Request cancelled | Other party (committee or requester) |
| New comment on a request | Other party |

Planned: missing-documents reminders, Wise verification failures, new events opened for applications.

## Common management commands

```bash
python manage.py migrate              # Apply database migrations
python manage.py seed_data            # Load demo fixture (empty DB only)
python manage.py set_demo_passwords   # Reset all passwords to "demo"
python manage.py check                # Validate project configuration
python manage.py runserver            # Start dev server
```

## Development notes

- **Database:** SQLite file at `db.sqlite3` in the project root.
- **Uploaded files:** stored under `uploads/` (served in development when `DEBUG=True`).
- **Static files:** served from `static/` during development.
- **Audit fields:** defined on abstract `AuditModel` in `marvin/audit.py`.
- **Login URLs:** the app uses `/login/` (username/password); Django admin uses `/admin/login/` separately.
- **UserProfile:** lives in the `profile` app (`profile.UserProfile`); database table remains `tsp_userprofile` for migration compatibility.

## Further reading

See [PRD.md](PRD.md) for the full product requirements and architecture documentation.
