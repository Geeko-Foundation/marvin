# Marvin - Travel Support Program (TSP)

Marvin is a Django web application for managing travel support requests: speakers apply for event reimbursement, committee members review and approve requests, and finance staff process payments. It includes a requestor signup wizard, Wise sandbox integration for banking verification, a pluggable payments module, and optional browser push notifications.

## Tech stack

- **Python 3.13+** (tested with 3.13)
- **Django 6.x**
- **SQLite** (default, development) or **PostgreSQL** (production)
- **django-tables2** - searchable data tables
- **django-webpush** - browser push notifications (opt-in)
- **DaisyUI / Tailwind** - UI styling (via templates)
- **Wise API** - sandbox recipient verification and payment initiation

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
| `webpush` | Third-party app - browser subscription storage and service worker (via django-webpush) |

All domain models in `profile`, `tsp`, and `payments` inherit from `AuditModel`, which adds automatic timestamp and user tracking on every save:

- `created_at` - set when the record is first saved
- `updated_at` - updated on every save
- `created_by` / `changed_by` - populated from the logged-in user via `AuditUserMiddleware`

These fields are defined once in `marvin/audit.py`; concrete models inherit them without re-declaring them in each `models.py`.

## Prerequisites

- Python 3.13 or newer
- `pip` and `venv`

## Quick start

### 1. Clone the repository

```bash
cd marvin
```

### 2. Install dependencies

**Recommended - [uv](https://github.com/astral-sh/uv):**

```bash
uv sync
```

This reads `pyproject.toml` and installs the exact versions pinned in `uv.lock` (Django, django-tables2, django-webpush, python-dotenv, requests, and their transitive deps).

**Alternative - pip:**

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Install **all** dependencies before running the server. Missing packages such as `django-tables2` or `django-webpush` cause startup failures.

### 3. Configure environment (optional)

```bash
cp .env.example .env
# edit .env for production; dev defaults in settings.py work without this
```

### 4. Run migrations

```bash
uv run python manage.py migrate
# or, with an activated venv: python manage.py migrate
```

### 5. Load demo data

```bash
uv run python manage.py seed_data
```

`seed_data` loads users, groups, profiles, and events from `tsp/fixtures/initial.json` and sets every user's password to **`demo`**. On a non-empty database it only resets passwords; pass **`--force`** to flush and reload demo data from scratch.

Alternatively, create a superuser manually:

```bash
python manage.py createsuperuser
```

### 6. Start the development server

```bash
uv run python manage.py runserver
```

Open [http://127.0.0.1:8000/login/](http://127.0.0.1:8000/login/) in your browser.

## Demo accounts

After seeding, all users share the password **`demo`**. Sign in with username and password at `/login/`.

| Username | Role | Notes |
|----------|------|-------|
| `admin` | TSP Committee, staff / superuser | Full Django admin access |
| `douglasd`, `jane_doe`, `patrickf` | Requester | Can submit travel requests; seeded with requests in several states |
| `finance_team` | Finance | Can process reimbursements |
| `superuser` | Requester, staff / superuser | Can skip Wise banking during onboarding |

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

- **Requester** - submit travel requests, complete profile and Wise onboarding
- **TSP Committee** - review, approve, reject, and manage events
- **Finance** - view reports and process reimbursements

Groups are created automatically after migrations via `profile.groups.ensure_groups()`.

## Wise integration (optional)

Wise runs in **sandbox mode** by default in development. `marvin.prod_settings` switches to the live API unless `WISE_API_SANDBOX=true` is set. Banking account numbers are sent to Wise only during verification - they are not stored in Marvin.

Staff and superusers may **skip Wise banking** during signup and complete it later from their profile.

Set `USE_WISE=false` to run without Wise at all. Sign-up then ends after the profile step, the Wise pages and webhook are switched off, and finance records payments made outside the app with the manual gateway.

`marvin/settings.py` still carries a published sandbox token as a fallback, so a deployment must set its own `WISE_API_TOKEN`. The published token should be treated as compromised.

Environment variables:

```bash
export WISE_API_TOKEN=your-token
export WISE_API_PROFILE_ID=          # auto-resolved when empty
export WISE_API_SANDBOX=true
export WISE_SOURCE_CURRENCY=EUR
```

Wise API activity is logged to the console and `logs/wise.log`.

## Push notifications

Marvin uses [django-webpush](https://github.com/safwanrahman/django-webpush) for browser push notifications. Users opt in from **Profile → Push Notifications**.

1. Check **Enable push notifications** and save.
2. Click **Subscribe to Push Messaging** and allow browser permission.

Notifications are only sent to users who have opted in and subscribed a browser. Disabling push in the profile removes stored subscriptions.

Dev VAPID keys are included in settings. They are public, so anyone with the repository could send pushes to a deployment that uses them. For production, generate your own (see `.env.example`) and set:

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
python manage.py seed_data            # Load demo fixture + set password "demo"
python manage.py seed_data --force    # Flush DB and reload demo data
python manage.py check                # Validate project configuration
python manage.py runserver            # Start dev server
```

## Rebranding for another organisation

Nothing about the name or colours is baked into the templates. A fork sets these
in `.env` and changes nothing else:

```bash
BRAND_APP_NAME=Wanderer
BRAND_ORG_NAME=GNOME Foundation
BRAND_TAGLINE=Conference Travel Subsidy
BRAND_THEME_LIGHT=emerald
BRAND_THEME_DARK=forest
BRAND_SOURCE_URL=https://github.com/gnome/wanderer
```

The two theme values are [daisyUI theme names](https://daisyui.com/docs/themes/).
Only themes listed in `css_build/app.css` are in the stylesheet, so add the pair
there and rebuild it (see `css_build/README.md`). The light and dark toggle keeps
working with whichever pair you choose.

## Environment variables

Configuration uses [python-dotenv](https://github.com/theskumar/python-dotenv). Values are read from the environment, or from a `.env` file at the project root (gitignored). On a host such as Railway, set them as service variables instead. `.env.example` lists every one with notes.

```bash
cp .env.example .env
```

Without them, `marvin/settings.py` falls back to **dev/test defaults** (insecure `SECRET_KEY`, Wise sandbox token, dev VAPID keys, SQLite). These are for local development only.

There are two settings modules:

- `marvin.settings` (the default) is for development and tests.
- `marvin.prod_settings` builds on it for production: `DEBUG` off, HTTPS only, two-step sign-in enforced, the live Wise API, and WhiteNoise compressed static files. It refuses to start without a real `DJANGO_SECRET_KEY` and a public `ALLOWED_HOSTS`. Select it with `DJANGO_SETTINGS_MODULE=marvin.prod_settings`.

| Variable | Purpose |
|----------|---------|
| `DJANGO_SETTINGS_MODULE` | `marvin.prod_settings` in production |
| `DJANGO_SECRET_KEY` | Django signing key (required in production) |
| `DEBUG` | `true` / `false` (always off in production) |
| `ALLOWED_HOSTS` | Comma-separated hostnames (required in production) |
| `CSRF_TRUSTED_ORIGINS` | Defaults to `https://` plus each public `ALLOWED_HOSTS` entry; set it only for plain http or a non-standard port |
| `DATABASE_ENGINE`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_HOST`, `DATABASE_PORT` | Database connection (see PostgreSQL) |
| `MEDIA_ROOT` | Where uploaded documents are stored |
| `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | SMTP mail (STARTTLS on port 587) |
| `SITE_BASE_URL` | Public site URL for links in emails |
| `USE_WISE` | `false` runs without Wise: no bank step at sign-up, payments recorded by hand |
| `WISE_API_TOKEN` | Wise API bearer token |
| `WISE_API_SANDBOX` | `true` for sandbox, `false` for live |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | Web push keys |
| `WEBPUSH_APP_BASE_URL` | Public site URL for notification links |
| `TWO_FACTOR_ENFORCED` | Require two-step sign-in (on in production) |

## PostgreSQL

SQLite is the default and needs no setup. For PostgreSQL, install the driver and point the app at your server:

```bash
uv sync --extra postgres
```

```bash
DATABASE_ENGINE=django.db.backends.postgresql
DATABASE_NAME=marvin
DATABASE_USER=marvin
DATABASE_PASSWORD=yourpassword
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_CONN_MAX_AGE=60      # seconds to reuse a connection; 0 closes each time
```

Then run `python manage.py migrate` and `python manage.py seed_data` as usual. CI runs the whole test suite against both SQLite and PostgreSQL.

`DATABASE_URL` is not read. Set the separate variables above.

### Railway

1. Add a PostgreSQL database to the project.
2. On the web service, set `DJANGO_SETTINGS_MODULE=marvin.prod_settings`.
3. Set the database variables from the PostgreSQL service's reference variables:

   ```bash
   DATABASE_ENGINE=django.db.backends.postgresql
   DATABASE_NAME=${{Postgres.PGDATABASE}}
   DATABASE_USER=${{Postgres.PGUSER}}
   DATABASE_PASSWORD=${{Postgres.PGPASSWORD}}
   DATABASE_HOST=${{Postgres.PGHOST}}
   DATABASE_PORT=${{Postgres.PGPORT}}
   ```

   Without them the app falls back to SQLite on the service's disk, which is replaced on every deploy.
4. Attach a volume (for example at `/data`) and set `MEDIA_ROOT=/data/uploads`, or uploaded receipts and forms are lost on each deploy.
5. Set `DJANGO_SECRET_KEY`, `ALLOWED_HOSTS`, `SITE_BASE_URL`, the email variables, `WISE_API_TOKEN` and the VAPID keys. Do not rely on the defaults in `marvin/settings.py`.
6. Set the start command to `python manage.py collectstatic --noinput && python manage.py migrate && python manage.py createcachetable && gunicorn marvin.wsgi`. Production serves static files through a manifest, so pages fail to render if `collectstatic` has not run. The cache table holds the sign-in limits, so nobody can sign in without it; running the command again is harmless. Leave `loaddata` out: on a lasting database it would reset every record in the fixture on each deploy.
7. Load the starting data once, from the service's shell: `python manage.py loaddata tsp/fixtures/initial.json`.
8. Two-step sign-in is enforced in production, so every user sets it up at their first sign in.

## Development notes

- **Database:** SQLite file at `db.sqlite3` in the project root. See PostgreSQL above to use that instead.
- **Uploaded files:** stored under `uploads/`, or `MEDIA_ROOT` when set (served in development when `DEBUG=True`).
- **Static files:** served from `static/` during development.
- **Audit fields:** defined on abstract `AuditModel` in `marvin/audit.py`.
- **Login URLs:** the app uses `/login/` (username/password); Django admin uses `/admin/login/` separately.
- **UserProfile:** lives in the `profile` app (`profile.UserProfile`); database table remains `tsp_userprofile` for migration compatibility.

## getting keys etc
https://randomkeygen.com/vapid-key
https://randomkeygen.com/django-secret-key

## License

Marvin is free software under the [GNU Affero General Public License v3](LICENSE).
Because it runs as a web service, anyone using a deployment must be able to get
its source code. Every page links to it in the footer; a fork that changes the
code sets `BRAND_SOURCE_URL` to its own repository.

## Further reading

See [PRD.md](PRD.md) for the full product requirements and architecture documentation.
