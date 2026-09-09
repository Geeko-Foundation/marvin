# Product Requirements Document — Marvin (TSP-NG)

> **Version:** 2.1  
> **Status:** Active development (Django rewrite)  
> **License:** AGPL v3  
> **Last updated:** 2026-09-09

---

## Table of Contents

1. [Overview & Background](#1-overview--background)
2. [Goals & Non-Goals](#2-goals--non-goals)
3. [User Groups & Personas](#3-user-groups--personas)
4. [System Architecture](#4-system-architecture)
5. [Data Model](#5-data-model)
6. [Request Lifecycle — State Machine](#6-request-lifecycle--state-machine)
7. [Feature Requirements](#7-feature-requirements)
8. [Route Reference](#8-route-reference)
9. [Authentication & Authorization](#9-authentication--authorization)
10. [Payment Integration — Wise](#10-payment-integration--wise)
11. [Push Notifications](#11-push-notifications)
12. [Document Management](#12-document-management)
13. [Reporting & Search](#13-reporting--search)
14. [Known Gaps & Limitations](#14-known-gaps--limitations)
15. [Roadmap](#15-roadmap)

---

## 1. Overview & Background

### 1.1 What is Marvin?

**Marvin** is a Django web application for managing travel sponsorship requests and expense reimbursements for free and open-source software (FOSS) organizations. It is the next-generation implementation of the Travel Support Program (TSP), developed by the openSUSE team at SUSE, and designed for reuse by organizations such as GNOME (Conference Travel Subsidy Program) and KDE e.V. (Travel Cost Reimbursement).

Marvin replaces an earlier Node.js/Express prototype (`prototype/`) and a legacy Ruby on Rails application (repository root) with a production-oriented Django stack.

### 1.2 Problem Statement

FOSS organizations routinely sponsor contributors to attend conferences, give talks, and represent the community. Manual processes lead to:

- **Opaque status** — requesters don't know where their request is in the pipeline
- **Slow turnaround** — manual handoffs between TSP committee, requester, and finance
- **Weak audit trail** — state changes and approvals are not systematically logged
- **Payment friction** — international transfers require manual SWIFT/IBAN entry and tracking
- **Reporting gaps** — no aggregate view of spend per event, per country, or over time

Marvin addresses these with a structured request lifecycle, role-based dashboards, Wise payment integration, browser push notifications, and full audit fields on all domain models.

### 1.3 Scope

This PRD describes **Marvin as implemented today** in the Django project at the repository root, plus known gaps and planned improvements. The legacy Rails app and Node.js prototype are retained for reference only.

---

## 2. Goals & Non-Goals

### 2.1 Goals

| # | Goal | Success Metric |
|---|---|---|
| G1 | **End-to-end request lifecycle** — submission through approval, acceptance, document upload, and payment | A requester can complete the full cycle in the app |
| G2 | **Group-based dashboards** — each user group sees actionable, scoped data | Three distinct dashboard views (Requester, TSP, Finance) |
| G3 | **Wise integration** — reduce international payment friction | Sandbox API for recipient verification and transfers; pluggable payment gateways |
| G4 | **Audit trail** — every state change and model mutation tracked | `created_by`, `changed_by`, `created_at`, `updated_at` on profile, TSP, and payment models |
| G5 | **Financial reporting** — aggregate spend by event, status, and date range | Filterable reports with CSV export |
| G6 | **Self-hostable** — small FOSS orgs can run their own instance | Django + SQLite; `manage.py runserver` for development |
| G7 | **Portable architecture** — not hardcoded to openSUSE | Configurable branding, plugin-style payments layer, separate `profile` app |
| G8 | **Privacy-conscious banking** — bank account numbers never stored locally | Only Wise recipient ID and metadata kept in Marvin |
| G9 | **Timely alerts** — users notified of request activity without polling | Opt-in browser push notifications on key state changes |

### 2.2 Non-Goals (current release)

| # | Non-Goal | Rationale |
|---|---|---|
| NG1 | Multi-tenant SaaS | Each org runs their own instance |
| NG2 | OAuth/SSO integration | Username/password login on app routes; SSO is roadmap |
| NG3 | Email notifications | Push notifications implemented; email is roadmap |
| NG4 | Budget caps per event/org | Useful but not blocking core workflow |
| NG5 | Mobile-native app | Responsive web (DaisyUI) is sufficient |
| NG6 | Multi-currency accounting | Reports default to EUR totals; conversion is roadmap |

---

## 3. User Groups & Personas

Access control uses **Django Groups and Permissions** (not a custom role field). Groups are managed in Django admin (`/admin/auth/group/`) and can be adjusted per deployment.

### 3.1 Group Definitions

| Group | Label | Permissions | Typical capabilities |
|---|---|---|---|
| `Requester` | Requester | `submit_travel_requests` | Sign up, complete onboarding, create/edit own requests, upload documents, accept approved requests, submit for payment |
| `TSP Committee` | TSP Committee | `submit_travel_requests`, `review_travel_requests`, `manage_events`, `view_reports` | Review queue, approve/reject with per-expense amounts, cancel requests, create events, view reports, create requests on behalf of contributors |
| `Finance` | Finance | `view_reports`, `process_payments` | Process payments via Wise API/CSV/manual, view payment queue and reports |

Users may belong to multiple groups. New sign-ups are assigned to **Requester** automatically.

Custom permissions are defined on `profile.UserProfile` and assigned to groups via `profile.groups.ensure_groups()`.

### 3.2 Personas

**Requester — "Douglas"**
- openSUSE contributor who speaks at conferences worldwide
- Signs up via a 3-step wizard (account → profile → Wise banking verification)
- Submits travel requests, tracks status, uploads receipts after the event
- Opts into push notifications for approval/rejection/payment updates

**TSP Committee — "Admin"**
- Reviews 5–20 requests per event cycle
- Needs a pending queue sorted by age and per-event summaries
- Receives push alerts when new requests are submitted
- Uses Django admin for data management when needed (`admin` / `demo`)

**Finance — "Finance Team"**
- Processes payments after events conclude
- Verifies required documents before paying
- Receives push alerts when requests enter the payment queue
- Uses the payments dashboard at `/payments/` for batch operations

---

## 4. System Architecture

### 4.1 High-Level Architecture

```
┌──────────────────────────────────────────────────────────────┐
│              Browser (Django templates + DaisyUI)            │
│  login/signup → dashboard → request detail → profile/push    │
└────────────────────────────┬─────────────────────────────────┘
                             │ HTTP
┌────────────────────────────▼─────────────────────────────────┐
│                    Django 6 (marvin project)                   │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐  │
│  │  tsp app    │  │ profile app  │  │ payments app        │  │
│  │  requests   │  │ UserProfile  │  │ gateways/adapters   │  │
│  │  events     │  │ groups       │  └─────────────────────┘  │
│  │  search     │  │ Wise services│  ┌─────────────────────┐  │
│  └─────────────┘  │ push service │  │ webpush (3rd party) │  │
│                   └──────────────┘  └─────────────────────┘  │
│  django.contrib.admin — /admin/                               │
├──────────────────────────────────────────────────────────────┤
│  marvin.audit — AuditModel + AuditUserMiddleware             │
│  profile.groups — Group setup and assignment helpers         │
├──────────────────────────────────────────────────────────────┤
│  SQLite (db.sqlite3) · File uploads (uploads/)               │
├──────────────────────────────────────────────────────────────┤
│  Wise Sandbox API · Browser Push (VAPID / django-webpush)    │
│  Logging: console + logs/wise.log                            │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 Technology Stack

| Layer | Technology | Notes |
|---|---|---|
| Runtime | Python 3.13+ | Virtualenv at `.venv/` |
| Framework | Django 6.1 | Server-side rendered HTML |
| Database | SQLite | `db.sqlite3`; suitable for dev/small deployments |
| Auth | Django auth + Groups | Username/password at `/login/`; admin at `/admin/login/` |
| UI | DaisyUI 4 + Tailwind (CDN) | Themes: `corporate` (light) / `business` (dark) |
| Tables | django-tables2 | Search page with DaisyUI table template |
| Push | django-webpush | VAPID keys; opt-in per user profile |
| File upload | Django `FileField` | Stored under `uploads/<request_id>/` |
| Payments | Pluggable gateway registry | Wise API, Wise CSV, manual transfer |
| HTTP client | `requests` | Wise REST API integration |
| Session | Django sessions | Database-backed (SQLite) |

### 4.3 Project Structure

```
marvin/                          # Django project settings
├── settings.py                  # App config, Wise, payments, webpush, logging
├── urls.py                      # /admin/, /webpush/, /payments/, /profile/, tsp
├── audit.py                     # AuditModel + AuditUserMiddleware
├── wsgi.py
profile/                         # User identity, groups, onboarding, push
├── models.py                    # UserProfile (db_table: tsp_userprofile)
├── views.py                     # Profile page
├── urls.py
├── forms.py                     # Profile, onboarding, Wise recipient forms
├── groups.py                    # Django group definitions and helpers
├── permissions.py               # finance_can_process_payments callback
├── decorators.py                # get_profile()
├── context_processors.py        # user_profile template context
├── admin.py                     # UserProfile admin + User inline
├── services/
│   ├── wise_verification.py     # 7-step Wise recipient verification
│   ├── wise_recipient.py        # Check/recreate Wise recipients
│   └── push_notifications.py    # Browser push on state changes
└── migrations/
tsp/                             # Travel Support Program core app
├── models.py                    # Event, TravelRequest, Expense, Document, …
├── views.py                     # TSP views (login, signup, requests, search)
├── urls.py
├── forms.py                     # Travel/expense forms, LoginForm, SignupAccountForm
├── decorators.py                # login_required, group_required, staff_or_owner
├── middleware.py                # Requester onboarding redirect
├── admin.py
├── tables.py                    # EventTable, UserTable (search)
├── payables.py                  # TravelRequestPayableAdapter
├── fixtures/initial.json        # Demo users, groups, profiles, events
├── services/
│   └── state_machine.py         # Request state transitions + push hooks
├── management/commands/
│   ├── seed_data.py             # Load fixture + ensure groups
│   └── set_demo_passwords.py    # Reset all passwords to "demo"
└── migrations/
payments/                        # Pluggable payments app
├── models.py                    # PaymentRecord (GenericForeignKey)
├── registry.py                  # Gateway/adapter plugin loader
├── services.py                  # PaymentService
├── gateways/
│   ├── wise_api.py              # Wise REST client (sandbox/production)
│   ├── wise.py                  # WiseApiGateway, WiseCsvGateway
│   └── manual.py                # ManualTransferGateway
└── views.py                     # /payments/ dashboard
templates/                       # Django templates (DaisyUI)
static/                          # Images, theme JS
uploads/                         # Document storage
logs/wise.log                    # Wise verification/payment logs
db.sqlite3                       # SQLite database
manage.py
README.md                        # Quick start and run instructions
PRD.md                           # This document
```

### 4.4 Deployment Model

Development:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_data        # first run only
python manage.py set_demo_passwords
python manage.py runserver
```

- App login: `http://127.0.0.1:8000/login/` (username + password, `demo` after seed)
- Django admin: `http://127.0.0.1:8000/admin/` (username `admin`, password `demo` after seed)
- Documents persist to `uploads/`
- Wise logs to `logs/wise.log` and console

Production deployment (roadmap): PostgreSQL, `DEBUG=False`, proper `SECRET_KEY`, HTTPS, production VAPID keys, and a WSGI server (gunicorn/uwsgi).

---

## 5. Data Model

### 5.1 Entity-Relationship Diagram

```
auth.User ──1:1── profile.UserProfile
     │
     ├──M:N── auth.Group (via user.groups)
     ├──1:N── webpush.PushInformation (browser subscriptions)
     │
     └──1:N── TravelRequest ──1:N── Expense
                      │
                      ├──1:N── StateHistory
                      ├──1:N── Comment
                      ├──1:N── Document
                      └──1:N── PaymentRecord (GenericRelation)

Event ──1:N── TravelRequest
```

All domain models in `profile`, `tsp`, and `payments` inherit **`AuditModel`** (`marvin/audit.py`):

| Field | Description |
|---|---|
| `created_at` | Auto-set on create |
| `created_by` | User who created the record |
| `updated_at` | Auto-updated on every save |
| `changed_by` | User who last modified the record |

Populated automatically via `AuditUserMiddleware` on authenticated requests. Fields are defined on the abstract base class and inherited by each concrete model.

### 5.2 Core Models

#### `profile.UserProfile` (extends `AuditModel`)

Stored in database table `tsp_userprofile` (legacy name retained after app split).

| Field | Type | Description |
|---|---|---|
| `user` | OneToOne → User | Django auth user |
| `phone`, `address_line`, `city`, `state`, `country`, `postal_code` | Text | Personal/contact info |
| `profile_completed_at` | DateTime, nullable | When profile wizard completed |
| `banking_method` | Choice | `iban`, `us_bank`, `india_ifsc` |
| `banking_currency` | Char(3) | Default payout currency |
| `wise_account_id` | Integer, nullable | Wise recipient account ID |
| `wise_recipient_verified_at` | DateTime, nullable | When Wise verification succeeded |
| `push_notifications_enabled` | Boolean | User opt-in for browser push (default `False`) |

**Not stored locally:** IBAN, SWIFT, account numbers, routing/IFSC codes. These exist only in the user's session during onboarding and are sent directly to Wise.

Custom permissions (assigned to groups):

- `submit_travel_requests`
- `review_travel_requests`
- `manage_events`
- `view_reports`
- `process_payments`

#### `Event`, `TravelRequest`, `Expense`, `StateHistory`, `Comment`, `Document` (tsp app)

Same semantics as before. `TravelRequest` orders by `-updated_at`.

#### `PaymentRecord` (payments app)

Gateway-agnostic payment log linked to any payable via `GenericForeignKey`:

| Field | Description |
|---|---|
| `content_type`, `object_id` | Payable object (e.g. TravelRequest) |
| `gateway_id` | `wise-api`, `wise-csv`, `manual` |
| `amount`, `currency`, `reference` | Payment details |
| `recipient_name`, `recipient_email` | Payee info |
| `external_id` | Wise transfer ID (when applicable) |
| `status`, `metadata` | Gateway-specific status |
| `processed_by` | Finance user who initiated payment |

#### Web Push models (django-webpush)

| Model | Description |
|---|---|
| `SubscriptionInfo` | Browser push endpoint and encryption keys |
| `PushInformation` | Links a subscription to a user (and optional group) |
| `Group` | Optional webpush group for broadcast subscriptions |

### 5.3 Seed Data

Run once on an empty database:

```bash
python manage.py seed_data
python manage.py set_demo_passwords
```

Fixture: `tsp/fixtures/initial.json` (users reference `profile.userprofile`).

**Users** (password `demo`):

| Username | Group | Notes |
|---|---|---|
| `admin` | TSP Committee | Also `is_staff` + `is_superuser` for Django admin |
| `requester` | Requester | Demo requester persona |
| `finance` | Finance | Payment processing |
| `superuser` | Requester | Staff superuser; can skip Wise banking |

**Events:** openSUSE Conference 2026/2027, FOSDEM 2026, LinuxConf.au 2026.

---

## 6. Request Lifecycle — State Machine

### 6.1 States

| State | Owner | Description |
|---|---|---|
| `draft` | Requester | Being edited; not visible to TSP |
| `submitted` | TSP queue | Awaiting committee review |
| `approved` | Requester | TSP approved with per-expense amounts; awaiting acceptance |
| `rejected` | Terminal | TSP rejected |
| `accepted` | Requester | Attendance confirmed; awaiting document uploads |
| `submitted_for_payment` | Finance queue | All required documents uploaded |
| `paid` | Terminal | Payment processed |
| `cancelled` | Terminal | Cancelled by requester or TSP |

### 6.2 Transitions

Implemented in `tsp/services/state_machine.py`. Each transition creates a `StateHistory` record and triggers push notifications (when recipients have opted in).

| From | To | Actor | Trigger |
|---|---|---|---|
| *(create)* | `draft` | Requester | POST `/requests/new/` |
| `draft` | `submitted` | Requester (owner) | POST `/requests/<id>/submit/` |
| `submitted` | `approved` | TSP | POST `/requests/<id>/approve/` |
| `submitted` | `rejected` | TSP | POST `/requests/<id>/reject/` |
| `approved` | `accepted` | Requester (owner) | POST `/requests/<id>/accept/confirm/` |
| `accepted` | `submitted_for_payment` | Requester (owner) | POST `/requests/<id>/submit-for-payment/` |
| `submitted_for_payment` | `paid` | Finance | POST `/payments/process/` |
| `draft`/`submitted`/`approved` | `cancelled` | Requester or TSP | POST `/requests/<id>/cancel/` |

### 6.3 Editability

| State | Requester can edit? |
|---|---|
| `draft`, `submitted` | Yes (own requests) |
| All other states | No |

---

## 7. Feature Requirements

### 7.1 Authentication & Sign-Up

**App login (`/login/`):**
- Standard username/password form (`LoginForm`)
- Session-based via Django auth
- Demo users use password `demo` after `set_demo_passwords`

**Self-service sign-up (`/signup/`):**
- Step 1: Account (username, email, password)
- Step 2: Profile (name, address, country)
- Step 3: Wise banking (method + account details held in **session only**)
- Step 4: 7-step Wise verification progress UI (see §10.2)
- Step 5: Completion page
- Staff/superusers may **skip Wise banking** (`/signup/banking/skip/`) and set up later

**Requester onboarding middleware** redirects incomplete requestors to the appropriate signup step until profile and Wise recipient are verified (admin routes exempt).

**Django admin (`/admin/`):**
- Standard username/password login at `/admin/login/`
- Full CRUD on users, groups, events, requests, payments
- User edit page includes group assignment; profile inline managed by `profile` app admin

### 7.2 Group-Based Dashboards

| Group | Template | Content |
|---|---|---|
| Requester | `tsp/dashboard/requester.html` | My requests, action-needed banners (approved/accepted) |
| TSP Committee | `tsp/dashboard/tsp.html` | Pending review queue, event summaries (90 days), state counts, approved total |
| Finance | `tsp/dashboard/finance.html` | Payment queue, recently paid, paid total |

### 7.3 Request Creation & Editing

- Event dropdown, talk fields, dynamic expense formset, description
- Expense categories: transportation, lodging, meals, registration, visa, other
- **Check Account** button (`POST /requests/check-account/`) — live Wise recipient status; auto-recreates stale recipients or redirects to banking step
- TSP members can also create requests (`group_required(Requester, TSP)`)

### 7.4 TSP Review & Approval

- Per-expense approval amounts and currency on request detail
- Approve/reject with notes
- Full state history visible on request detail
- Push notification to requester on approve/reject

### 7.5 Requester Acceptance

- Dedicated acceptance page with required checkbox confirmations
- Moves request to `accepted` state
- Push reminder to upload documents

### 7.6 Comments & Documents

- Comment thread on request detail (requester + committee)
- Push notification to the other party when a comment is added
- Document upload with kind selector
- Download with owner/committee permission check
- All three document kinds required before submit-for-payment

### 7.7 Events

- List with optional date filters
- Create event form
- Past events dimmed; "Apply" CTA for requesters on open events

### 7.8 Profile, Banking & Notifications

**All users (`/profile/`):**
- Personal info editable on profile page
- **Push notifications** opt-in checkbox; browser subscribe button when enabled
- Disabling push removes stored browser subscriptions

**Requesters:**
- Wise recipient status shown (account ID, verification date) — no raw bank numbers displayed
- Re-verification via signup banking flow if recipient is missing at Wise

**TSP/Finance staff:**
- Optional staff banking form (Wise metadata only, no local account storage)

### 7.9 Search

- `/search/` — event search for all authenticated users (django-tables2)
- User search table visible to staff only
- Navbar search shortcut and sidebar link

### 7.10 UI & Theming

- DaisyUI component library with responsive drawer layout
- Toggle-able dark mode (`corporate` / `business` themes) via theme scripts and localStorage
- Theme toggle in navbar (authenticated) and fixed top-right (login/signup)
- Login page uses full-screen background image (`static/img/waiting.jpg`)

### 7.11 Printable Reimbursement Form

- `/requests/<id>/form/` — printable agreement with approved expenses
- Print-friendly CSS

---

## 8. Route Reference

### 8.1 Authentication & Sign-Up

| Method | Path | Auth | Group | Description |
|---|---|---|---|---|
| GET/POST | `/login/` | No | — | Username/password login |
| GET | `/logout/` | Yes | Any | Logout |
| GET/POST | `/signup/` | No* | — | Account creation |
| GET/POST | `/signup/profile/` | Yes | Requester | Profile step |
| GET/POST | `/signup/banking/` | Yes | Requester | Banking step |
| GET | `/signup/banking/progress/` | Yes | Requester | Verification progress UI |
| POST | `/signup/banking/step/` | Yes | Requester | Run one Wise verification step (JSON) |
| POST | `/signup/banking/skip/` | Yes | Requester (staff) | Skip Wise banking |
| GET | `/signup/complete/` | Yes | Requester | Wizard complete |

### 8.2 Core TSP Routes

| Method | Path | Auth | Group | Description |
|---|---|---|---|---|
| GET | `/` | Yes | Any | Role-specific dashboard |
| GET/POST | `/profile/` | Yes | Any | Profile, banking, push settings |
| GET | `/search/` | Yes | Any | Event/user search |
| GET | `/events/` | Yes | Any | Event list |
| GET/POST | `/events/new/` | Yes | Any | Create event |
| GET/POST | `/requests/new/` | Yes | Requester, TSP | Create request |
| POST | `/requests/check-account/` | Yes | Requester, TSP | Wise recipient check (JSON) |
| GET | `/requests/<id>/` | Yes | Owner or committee | Request detail |
| GET/POST | `/requests/<id>/edit/` | Yes | Owner (draft/submitted) | Edit request |
| POST | `/requests/<id>/submit/` | Yes | Owner | Submit for review |
| POST | `/requests/<id>/approve/` | Yes | TSP | Approve |
| POST | `/requests/<id>/reject/` | Yes | TSP | Reject |
| POST | `/requests/<id>/cancel/` | Yes | Owner or TSP | Cancel |
| GET/POST | `/requests/<id>/accept/` | Yes | Owner | Acceptance flow |
| POST | `/requests/<id>/submit-for-payment/` | Yes | Owner | Submit for payment |
| POST | `/requests/<id>/comments/` | Yes | Owner or committee | Add comment |
| POST | `/requests/<id>/documents/` | Yes | Owner or committee | Upload document |
| GET | `/requests/<id>/form/` | Yes | Owner or committee | Printable form |
| GET | `/documents/<id>/download/` | Yes | Owner or committee | Download document |
| GET | `/reports/` | Yes | TSP, Finance | Reports (+ `?format=csv`) |

### 8.3 Payment Routes (`/payments/`)

| Method | Path | Auth | Group | Description |
|---|---|---|---|---|
| GET | `/payments/` | Yes | Finance | Payment dashboard |
| POST | `/payments/process/` | Yes | Finance | Process single payment |
| POST | `/payments/batch-export/` | Yes | Finance | Wise batch CSV download |
| GET | `/payments/<id>/status/` | Yes | Finance | Payment status (JSON) |
| GET | `/payments/gateways/<id>/currencies/` | Yes | Finance | Supported currencies (JSON) |

### 8.4 Web Push Routes (`/webpush/`)

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/webpush/save_information` | Yes | Save browser subscription |
| GET | `/webpush/service-worker.js` | No | Service worker script |

### 8.5 Admin

| Path | Description |
|---|---|
| `/admin/` | Django admin (users, groups, all models) |

---

## 9. Authentication & Authorization

### 9.1 Dual Login Model

| Context | Login URL | Method |
|---|---|---|
| Marvin app | `/login/` | Username + password (`APP_LOGIN_URL`) |
| Django admin | `/admin/login/` | Username + password (`LOGIN_URL`) |

App views use `@login_required` with `APP_LOGIN_URL`. Admin uses Django's built-in auth.

### 9.2 Authorization Mechanisms

1. **Django Groups** — primary access control (`profile/groups.py`)
2. **Custom permissions** — on `profile.UserProfile`; checkable via `user.has_perm('profile.process_payments')`
3. **View decorators** — `@group_required(...)`, `@staff_or_owner` (owner or TSP/Finance)
4. **Payment plugin callback** — `PAYMENTS_PERMISSION_CHECK = 'profile.permissions.finance_can_process_payments'`

### 9.3 Authorization Matrix

| Action | Requester | TSP | Finance |
|---|---|---|---|
| Sign up / onboard | ✓ | — | — |
| Create request | ✓ | ✓ | — |
| Edit own request (draft/submitted) | ✓ | — | — |
| Approve/reject | — | ✓ | — |
| Cancel request | ✓ (own) | ✓ | — |
| Accept / submit for payment | ✓ (own) | — | — |
| Upload/download documents | ✓ (own) | ✓ | ✓ |
| Process payment | — | — | ✓ |
| View reports | — | ✓ | ✓ |
| Enable push notifications | ✓ | ✓ | ✓ |
| Django admin | — | ✓* | — |

\* Requires `is_staff` / `is_superuser` in addition to group membership.

---

## 10. Payment Integration — Wise

### 10.1 Architecture

Payments use a **plugin registry** (`payments/registry.py`):

- **Gateways** — how money moves (`WiseApiGateway`, `WiseCsvGateway`, `ManualTransferGateway`)
- **Adapters** — how payables expose amount, recipient, reference (`TravelRequestPayableAdapter`)
- **PaymentRecord** — audit log of every processed payment

Configured in `settings.py`:

```python
PAYMENTS_GATEWAYS = [
    'payments.gateways.wise.WiseApiGateway',
    'payments.gateways.wise.WiseCsvGateway',
    'payments.gateways.manual.ManualTransferGateway',
]
PAYMENTS_ADAPTERS = ['tsp.payables.TravelRequestPayableAdapter']
PAYMENTS_PERMISSION_CHECK = 'profile.permissions.finance_can_process_payments'
```

### 10.2 Wise Recipient Verification (Onboarding)

Seven-step verification against the **Wise sandbox API** (`profile/services/wise_verification.py`):

1. Create Wise payee account
2. Confirm bank details with Wise
3. Request exchange quote for test transfer
4. Create test transfer
5. Fund test transfer from Wise balance (with sandbox simulation fallback)
6. Cancel/reverse test transfer
7. Save verification to profile (`wise_account_id`, `wise_recipient_verified_at`)

Bank details are passed from session → Wise API only. Progress UI polls `POST /signup/banking/step/` per step. Logs written to `marvin.wise` logger (console + `logs/wise.log`).

### 10.3 Wise Recipient Check (Request Form)

`POST /requests/check-account/` (`profile/services/wise_recipient.py`):

- Confirms recipient still exists at Wise
- On 403/404: clears stale `wise_account_id`, returns `needs_recreate` with redirect to banking step
- Optional inline recreate with bank details from POST

### 10.4 Payment Processing

| Gateway | ID | Description |
|---|---|---|
| Wise API | `wise-api` | Live transfer via REST API |
| Wise CSV | `wise-csv` | Batch `Send-by-email.csv` export |
| Manual | `manual` | Record payment without API call |

Completing payment transitions the travel request to `paid` and sends a push notification to the requester.

### 10.5 Configuration

| Setting | Default | Description |
|---|---|---|
| `WISE_API_TOKEN` | Sandbox token | Bearer token (env override) |
| `WISE_API_PROFILE_ID` | *(auto)* | Wise business profile ID |
| `WISE_API_SANDBOX` | `true` | Use `api.wise-sandbox.com` |
| `WISE_SOURCE_CURRENCY` | `GBP` | Source currency for quotes/transfers |

---

## 11. Push Notifications

### 11.1 Overview

Marvin uses **django-webpush** for browser push notifications. Users must:

1. Enable **Push notifications** on their profile (`push_notifications_enabled`)
2. Click **Subscribe to Push Messaging** and grant browser permission

Disabling push in the profile deletes stored browser subscriptions.

Implementation: `profile/services/push_notifications.py`, hooked from `tsp/services/state_machine.py` and comment creation.

### 11.2 Notification Events

| Event | Recipient | Trigger |
|---|---|---|
| Request submitted | TSP Committee | State → `submitted` |
| Request approved | Requester | State → `approved` |
| Request rejected | Requester | State → `rejected` |
| Request accepted | Requester | State → `accepted` (upload docs reminder) |
| Submitted for payment | Finance | State → `submitted_for_payment` |
| Payment processed | Requester | State → `paid` |
| Request cancelled | Other party | State → `cancelled` |
| New comment | Other party | Comment posted on request |

### 11.3 Planned Push Events

| Event | Recipient |
|---|---|
| Missing required documents | Requester (before payment submission) |
| Wise verification failed | Requester |
| New event accepting requests | Requesters |
| Payment queue item aging | Finance |

### 11.4 Configuration

| Setting | Description |
|---|---|
| `WEBPUSH_SETTINGS.VAPID_PUBLIC_KEY` | Browser application server key |
| `WEBPUSH_SETTINGS.VAPID_PRIVATE_KEY` | Server signing key |
| `WEBPUSH_SETTINGS.VAPID_ADMIN_EMAIL` | Contact for push service providers |
| `WEBPUSH_APP_BASE_URL` | Base URL for notification click-through links |

Dev keys are included in settings; production must use environment variables.

---

## 12. Document Management

### 12.1 Document Types

| Kind | Required for payment |
|---|---|
| `receipt` | Yes |
| `invoice` | Yes |
| `signed_form` | Yes |

### 12.2 Storage

- Files: `uploads/<request_id>/<filename>` via Django `FileField`
- Metadata in `Document` model with audit fields
- Download restricted to request owner or committee members

### 12.3 Gaps

| Gap | Target |
|---|---|
| No MIME whitelist | Restrict to PDF, JPG, PNG |
| No size limits | 10 MB per file |
| Disk-only storage | S3/R2-compatible backend |

---

## 13. Reporting & Search

### 13.1 Reports

**Available to:** TSP Committee and Finance groups

**Filters:** Event date range, request status

**Output:** HTML table + CSV export (`?format=csv`)

**Summary cards:** Total requests, total approved, total paid

### 13.2 Search

**Available to:** All authenticated users (events); staff (users)

**Implementation:** django-tables2 with custom DaisyUI template

**Fields:** Event name/location; user username/name/email (staff only)

---

## 14. Known Gaps & Limitations

### 14.1 Security & Auth

| Gap | Notes |
|---|---|
| Demo password `demo` | All seeded users share one password; change for production |
| Insecure `SECRET_KEY` | Default dev key; must change for production |
| `DEBUG=True` | Must disable in production |
| Dev VAPID keys in settings | Must rotate for production |

### 14.2 Features

| Gap | Notes |
|---|---|
| No email notifications | Push implemented; email still roadmap |
| No pagination | Lists capped (~20–100 items) |
| No event edit/delete UI | Admin only |
| No batch approve/reject | One request at a time |
| No internal notes | All comments visible to requester |
| Push requires HTTPS in production | Browsers require secure context for subscriptions |

### 14.3 Operations

| Gap | Notes |
|---|---|
| SQLite | Fine for dev; PostgreSQL recommended for production |
| No automated tests | Test suite not yet implemented |
| No CI/CD | Manual deploy |

### 14.4 Wise

| Gap | Notes |
|---|---|
| Sandbox by default | Production requires live API credentials and compliance review |
| No webhook reconciliation | Status polled manually |
| No exchange rate locking | Rate may change between approval and payment |

---

## 15. Roadmap

### Phase 1 — Production Hardening

| Task | Priority | Status |
|---|---|---|
| Username/password login on app routes | Critical | Done |
| Separate `profile` app for UserProfile | High | Done |
| Audit timestamps (`created_at`, `updated_at`) on all domain models | High | Done |
| PostgreSQL support | High | Planned |
| Environment-based secrets (`SECRET_KEY`, `WISE_API_TOKEN`, VAPID keys) | High | Partial |
| File upload validation (type, size) | High | Planned |
| Pagination on list views | Medium | Planned |
| Automated test suite | High | Planned |

### Phase 2 — Feature Completion

| Task | Priority | Status |
|---|---|---|
| Browser push notifications | High | Done |
| Email notifications (request state changes) | High | Planned |
| Additional push events (docs missing, new events) | Medium | Planned |
| Event edit/delete in UI | Medium | Planned |
| Batch TSP approve/reject | Medium | Planned |
| Internal notes (TSP/Finance only) | Medium | Planned |
| Production Wise API + webhooks | High | Planned |

### Phase 3 — Scale & Polish

| Task | Priority |
|---|---|
| SSO/OAuth (Keycloak, GitHub) | Medium |
| REST/JSON API layer | Medium |
| Multi-org / configurable branding | Medium |
| Server-side PDF generation | Low |
| i18n | Low |
| WCAG 2.1 accessibility audit | Medium |

---

## Appendix A: Environment Variables

| Variable | Default | Description |
|---|---|---|
| `WISE_API_TOKEN` | Sandbox token in settings | Wise API bearer token |
| `WISE_API_PROFILE_ID` | *(empty, auto-resolved)* | Wise business profile ID |
| `WISE_API_SANDBOX` | `true` | Use Wise sandbox API |
| `WISE_SOURCE_CURRENCY` | `GBP` | Source currency for transfers |
| `VAPID_PUBLIC_KEY` | Dev key in settings | Web push public key |
| `VAPID_PRIVATE_KEY` | Dev key in settings | Web push private key |
| `VAPID_ADMIN_EMAIL` | `admin@marvin.local` | Push service contact email |
| `WEBPUSH_APP_BASE_URL` | `http://127.0.0.1:8000` | Notification link base URL |
| `DJANGO_SECRET_KEY` | *(dev default)* | **Required for production** |
| `DATABASE_URL` | SQLite file | **Planned:** PostgreSQL connection |

## Appendix B: Management Commands

| Command | Description |
|---|---|
| `python manage.py migrate` | Apply database migrations |
| `python manage.py seed_data` | Load demo users, groups, profiles, and events from fixture |
| `python manage.py set_demo_passwords` | Set all user passwords to `demo` |
| `python manage.py createsuperuser` | Create additional admin users |
| `python manage.py check` | Validate project configuration |

## Appendix C: Expense Categories

| Category | Label |
|---|---|
| `transportation` | Transportation |
| `lodging` | Lodging |
| `meals` | Meals |
| `registration` | Registration |
| `visa` | Visa Fees |
| `other` | Other |

## Appendix D: Legacy Code

| Path | Status |
|---|---|
| Repository root (Rails) | Legacy reference |
| `prototype/` | Superseded Node.js prototype (Express + JSON file DB) |

Marvin (Django) is the active implementation described in this document.
