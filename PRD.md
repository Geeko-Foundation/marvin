# Product Requirements Document — Marvin (TSP-NG)

> **Version:** 2.0  
> **Status:** Active development (Django rewrite)  
> **License:** AGPL v3  
> **Last updated:** 2026-09-08

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
11. [Document Management](#11-document-management)
12. [Reporting](#12-reporting)
13. [Known Gaps & Limitations](#13-known-gaps--limitations)
14. [Roadmap](#14-roadmap)

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

Marvin addresses these with a structured request lifecycle, role-based dashboards, Wise payment integration, and full audit fields on all domain models.

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
| G4 | **Audit trail** — every state change and model mutation tracked | `created_by`, `changed_by`, `created_at`, `changed_at` on TSP and payment models |
| G5 | **Financial reporting** — aggregate spend by event, status, and date range | Filterable reports with CSV export |
| G6 | **Self-hostable** — small FOSS orgs can run their own instance | Django + SQLite; `manage.py runserver` for development |
| G7 | **Portable architecture** — not hardcoded to openSUSE | Configurable branding, plugin-style payments layer |
| G8 | **Privacy-conscious banking** — bank account numbers never stored locally | Only Wise recipient ID and metadata kept in Marvin |

### 2.2 Non-Goals (current release)

| # | Non-Goal | Rationale |
|---|---|---|
| NG1 | Multi-tenant SaaS | Each org runs their own instance |
| NG2 | OAuth/SSO integration | Demo login sufficient for now; SSO is roadmap |
| NG3 | Email notifications | Not wired up; users check dashboard manually |
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

### 3.2 Personas

**Requester — "Douglas"**
- openSUSE contributor who speaks at conferences worldwide
- Signs up via a 3-step wizard (account → profile → Wise banking verification)
- Submits travel requests, tracks status, uploads receipts after the event
- Needs clear CTAs on the dashboard for next actions

**TSP Committee — "Admin"**
- Reviews 5–20 requests per event cycle
- Needs a pending queue sorted by age and per-event summaries
- Uses Django admin for data management when needed (`admin` / `demo`)

**Finance — "Finance Team"**
- Processes payments after events conclude
- Verifies required documents before paying
- Uses the payments dashboard at `/payments/` for batch operations

---

## 4. System Architecture

### 4.1 High-Level Architecture

```
┌──────────────────────────────────────────────────────────────┐
│              Browser (Django templates + DaisyUI)            │
│  login/signup → dashboard → request detail → payments        │
└────────────────────────────┬─────────────────────────────────┘
                             │ HTTP
┌────────────────────────────▼─────────────────────────────────┐
│                    Django 6 (marvin project)                   │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐  │
│  │  tsp app    │  │ payments app │  │ django.contrib.admin│  │
│  │  views      │  │  gateways    │  │  /admin/            │  │
│  │  services   │  │  adapters    │  └─────────────────────┘  │
│  │  middleware │  │  registry    │                            │
│  └─────────────┘  └──────────────┘                            │
├──────────────────────────────────────────────────────────────┤
│  marvin.audit — AuditModel + AuditUserMiddleware             │
│  tsp.groups   — Group setup and assignment helpers           │
├──────────────────────────────────────────────────────────────┤
│  SQLite (db.sqlite3) · File uploads (uploads/)               │
├──────────────────────────────────────────────────────────────┤
│  Wise Sandbox API (payments/gateways/wise_api.py)            │
│  Logging: console + logs/wise.log                            │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 Technology Stack

| Layer | Technology | Notes |
|---|---|---|
| Runtime | Python 3.13+ | Virtualenv at `.venv/` |
| Framework | Django 6.1 | Server-side rendered HTML |
| Database | SQLite | `db.sqlite3`; suitable for dev/small deployments |
| Auth | Django auth + Groups | Demo click-login for app; password login at `/admin/login/` |
| UI | DaisyUI 4 + Tailwind (CDN) | Themes: `corporate` (light) / `business` (dark) |
| File upload | Django `FileField` | Stored under `uploads/<request_id>/` |
| Payments | Pluggable gateway registry | Wise API, Wise CSV, manual transfer |
| HTTP client | `requests` | Wise REST API integration |
| Session | Django sessions | Database-backed (SQLite) |

### 4.3 Project Structure

```
marvin/                          # Django project settings
├── settings.py                  # App config, Wise, payments, logging
├── urls.py                      # /admin/, /payments/, tsp routes
├── audit.py                     # AuditModel + middleware
├── wsgi.py
tsp/                             # Travel Support Program core app
├── models.py                    # UserProfile, Event, TravelRequest, …
├── views.py                     # All TSP views
├── urls.py
├── forms.py
├── decorators.py                # login_required, group_required, staff_or_owner
├── groups.py                    # Django group definitions and helpers
├── permissions.py               # Payment permission callback
├── middleware.py                # Requester onboarding redirect
├── admin.py
├── payables.py                  # TravelRequestPayableAdapter
├── services/
│   ├── state_machine.py         # Request state transitions
│   ├── wise_verification.py     # 7-step Wise recipient verification
│   └── wise_recipient.py        # Check/recreate Wise recipients
├── management/commands/
│   └── seed_data.py             # Demo users, events, groups
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
static/js/theme.js               # Dark mode toggle (localStorage)
uploads/                         # Document storage
logs/wise.log                    # Wise verification/payment logs
db.sqlite3                       # SQLite database
manage.py
PRD.md                           # This document
```

### 4.4 Deployment Model

Development:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # if present
python manage.py migrate
python manage.py seed_data        # first run only
python manage.py runserver
```

- App login: `http://127.0.0.1:8000/login/`
- Django admin: `http://127.0.0.1:8000/admin/` (username `admin`, password `demo` after seed)
- Documents persist to `uploads/`
- Wise logs to `logs/wise.log` and console

Production deployment (roadmap): PostgreSQL, `DEBUG=False`, proper `SECRET_KEY`, HTTPS, and a WSGI server (gunicorn/uwsgi).

---

## 5. Data Model

### 5.1 Entity-Relationship Diagram

```
auth.User ──1:1── UserProfile
     │
     ├──M:N── auth.Group (via user.groups)
     │
     └──1:N── TravelRequest ──1:N── Expense
                      │
                      ├──1:N── StateHistory
                      ├──1:N── Comment
                      ├──1:N── Document
                      └──1:N── PaymentRecord (GenericRelation)

Event ──1:N── TravelRequest
```

All TSP models and `PaymentRecord` inherit **`AuditModel`** with:

| Field | Description |
|---|---|
| `created_at` | Auto-set on create |
| `created_by` | User who created the record |
| `changed_at` | Auto-updated on save |
| `changed_by` | User who last modified the record |

Populated automatically via `AuditUserMiddleware` on authenticated requests.

### 5.2 Core Models

#### `UserProfile` (extends `AuditModel`)

| Field | Type | Description |
|---|---|---|
| `user` | OneToOne → User | Django auth user |
| `phone`, `address_line`, `city`, `state`, `country`, `postal_code` | Text | Personal/contact info |
| `profile_completed_at` | DateTime, nullable | When profile wizard completed |
| `banking_method` | Choice | `iban`, `us_bank`, `india_ifsc` |
| `banking_currency` | Char(3) | Default payout currency |
| `wise_account_id` | Integer, nullable | Wise recipient account ID |
| `wise_recipient_verified_at` | DateTime, nullable | When Wise verification succeeded |

**Not stored locally:** IBAN, SWIFT, account numbers, routing/IFSC codes. These exist only in the user's session during onboarding and are sent directly to Wise.

Custom permissions (assigned to groups):

- `submit_travel_requests`
- `review_travel_requests`
- `manage_events`
- `view_reports`
- `process_payments`

#### `Event`

| Field | Description |
|---|---|
| `name`, `location` | Event identity |
| `start_date`, `end_date` | Date range |
| `accepting_requests` | Whether new requests can be created |

#### `TravelRequest`

| Field | Description |
|---|---|
| `user` | Requester (FK → User) |
| `event` | Target event |
| `talk_url`, `talk_title`, `description` | Request details |
| `state` | Lifecycle state (see §6) |
| `approved_at`, `accepted_at`, `accepted_by` | Approval/acceptance metadata |
| `payments` | GenericRelation to `PaymentRecord` |

#### `Expense`

| Field | Description |
|---|---|
| `request` | Parent travel request |
| `category` | `transportation`, `lodging`, `meals`, `registration`, `visa`, `other` |
| `description` | Free text |
| `estimated_amount`, `estimated_currency` | Requester estimate |
| `approved_amount`, `approved_currency` | TSP-approved amount (nullable until approval) |

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

#### `StateHistory`, `Comment`, `Document`

Same semantics as the original TSP design. Documents stored at `uploads/<request_id>/<filename>` with kinds: `receipt`, `invoice`, `signed_form`.

### 5.3 Seed Data

Run once on an empty database:

```bash
python manage.py seed_data
```

**Users** (password `demo` for admin login; app uses click-login):

| Username | Group | Notes |
|---|---|---|
| `admin` | TSP Committee | Also `is_staff` + `is_superuser` for Django admin |
| `requester` | Requester | Demo requester persona |
| `finance` | Finance | Payment processing |

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

Implemented in `tsp/services/state_machine.py`:

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

Every transition creates a `StateHistory` record with actor and optional notes.

### 6.3 Editability

| State | Requester can edit? |
|---|---|
| `draft`, `submitted` | Yes (own requests) |
| All other states | No |

---

## 7. Feature Requirements

### 7.1 Authentication & Sign-Up

**App login (`/login/`):**
- Demo click-login: select a pre-seeded user (no password)
- Session-based via Django auth

**Self-service sign-up (`/signup/`):**
- Step 1: Account (username, email, password)
- Step 2: Profile (name, address, country)
- Step 3: Wise banking (method + account details held in **session only**)
- Step 4: 7-step Wise verification progress UI (see §10.2)
- Step 5: Completion page

**Requester onboarding middleware** redirects incomplete requesters to the appropriate signup step until profile and Wise recipient are verified.

**Django admin (`/admin/`):**
- Standard username/password login at `/admin/login/`
- Full CRUD on users, groups, events, requests, payments
- User edit page includes group assignment and inline profile

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

### 7.5 Requester Acceptance

- Dedicated acceptance page with required checkbox confirmations
- Moves request to `accepted` state

### 7.6 Comments & Documents

- Comment thread on request detail (requester + committee)
- Document upload with kind selector
- Download with owner/committee permission check
- All three document kinds required before submit-for-payment

### 7.7 Events

- List with optional date filters
- Create event form
- Past events dimmed; "Apply" CTA for requesters on open events

### 7.8 Profile & Banking

**Requesters:**
- Personal info editable on profile page
- Wise recipient status shown (account ID, verification date) — no raw bank numbers displayed
- Re-verification via signup banking flow if recipient is missing at Wise

**TSP/Finance staff:**
- Personal info + optional staff banking form (Wise metadata only, no local account storage)

### 7.9 UI & Theming

- DaisyUI component library with responsive drawer layout
- Toggle-able dark mode (`corporate` / `business` themes) via `static/js/theme.js` and localStorage
- Theme toggle in navbar (authenticated) and fixed top-right (login/signup)

### 7.10 Printable Reimbursement Form

- `/requests/<id>/form/` — printable agreement with approved expenses
- Print-friendly CSS

---

## 8. Route Reference

### 8.1 Authentication & Sign-Up

| Method | Path | Auth | Group | Description |
|---|---|---|---|---|
| GET | `/login/` | No | — | Demo login page |
| POST | `/login/submit/` | No | — | Click-login |
| GET | `/logout/` | Yes | Any | Logout |
| GET/POST | `/signup/` | No* | — | Account creation |
| GET/POST | `/signup/profile/` | Yes | Requester | Profile step |
| GET/POST | `/signup/banking/` | Yes | Requester | Banking step |
| GET | `/signup/banking/progress/` | Yes | Requester | Verification progress UI |
| POST | `/signup/banking/step/` | Yes | Requester | Run one Wise verification step (JSON) |
| GET | `/signup/complete/` | Yes | Requester | Wizard complete |

### 8.2 Core TSP Routes

| Method | Path | Auth | Group | Description |
|---|---|---|---|---|
| GET | `/` | Yes | Any | Role-specific dashboard |
| GET/POST | `/profile/` | Yes | Any | Profile management |
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

### 8.4 Admin

| Path | Description |
|---|---|
| `/admin/` | Django admin (users, groups, all models) |

---

## 9. Authentication & Authorization

### 9.1 Dual Login Model

| Context | Login URL | Method |
|---|---|---|
| Marvin app | `/login/` | Demo click-login (`APP_LOGIN_URL`) |
| Django admin | `/admin/login/` | Username + password (`LOGIN_URL`) |

App views use `@login_required` with `APP_LOGIN_URL`. Admin uses Django's built-in auth.

### 9.2 Authorization Mechanisms

1. **Django Groups** — primary access control (`tsp/groups.py`)
2. **Custom permissions** — assigned to groups; checkable via `user.has_perm('tsp.process_payments')`
3. **View decorators** — `@group_required(...)`, `@staff_or_owner` (owner or TSP/Finance)
4. **Payment plugin callback** — `PAYMENTS_PERMISSION_CHECK = 'tsp.permissions.finance_can_process_payments'`

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
PAYMENTS_PERMISSION_CHECK = 'tsp.permissions.finance_can_process_payments'
```

### 10.2 Wise Recipient Verification (Onboarding)

Seven-step verification against the **Wise sandbox API** (`tsp/services/wise_verification.py`):

1. Create Wise payee account
2. Confirm bank details with Wise
3. Request exchange quote for test transfer
4. Create test transfer
5. Fund test transfer from Wise balance (with sandbox simulation fallback)
6. Cancel/reverse test transfer
7. Save verification to profile (`wise_account_id`, `wise_recipient_verified_at`)

Bank details are passed from session → Wise API only. Progress UI polls `POST /signup/banking/step/` per step. Logs written to `marvin.wise` logger (console + `logs/wise.log`).

### 10.3 Wise Recipient Check (Request Form)

`POST /requests/check-account/` (`tsp/services/wise_recipient.py`):

- Confirms recipient still exists at Wise
- On 403/404: clears stale `wise_account_id`, returns `needs_recreate` with redirect to banking step
- Optional inline recreate with bank details from POST

### 10.4 Payment Processing

| Gateway | ID | Description |
|---|---|---|
| Wise API | `wise-api` | Live transfer via REST API |
| Wise CSV | `wise-csv` | Batch `Send-by-email.csv` export |
| Manual | `manual` | Record payment without API call |

### 10.5 Configuration

| Setting | Default | Description |
|---|---|---|
| `WISE_API_TOKEN` | Sandbox token | Bearer token (env override) |
| `WISE_API_PROFILE_ID` | *(auto)* | Wise business profile ID |
| `WISE_API_SANDBOX` | `true` | Use `api.wise-sandbox.com` |
| `WISE_SOURCE_CURRENCY` | `GBP` | Source currency for quotes/transfers |

---

## 11. Document Management

### 11.1 Document Types

| Kind | Required for payment |
|---|---|
| `receipt` | Yes |
| `invoice` | Yes |
| `signed_form` | Yes |

### 11.2 Storage

- Files: `uploads/<request_id>/<filename>` via Django `FileField`
- Metadata in `Document` model with audit fields
- Download restricted to request owner or committee members

### 11.3 Gaps

| Gap | Target |
|---|---|
| No MIME whitelist | Restrict to PDF, JPG, PNG |
| No size limits | 10 MB per file |
| Disk-only storage | S3/R2-compatible backend |

---

## 12. Reporting

**Available to:** TSP Committee and Finance groups

**Filters:** Event date range, request status

**Output:** HTML table + CSV export (`?format=csv`)

**Summary cards:** Total requests, total approved, total paid

---

## 13. Known Gaps & Limitations

### 13.1 Security & Auth

| Gap | Notes |
|---|---|
| Demo click-login | No password on app routes; production needs real login |
| Insecure `SECRET_KEY` | Default dev key; must change for production |
| `DEBUG=True` | Must disable in production |

### 13.2 Features

| Gap | Notes |
|---|---|
| No email notifications | Users must check dashboard |
| No pagination | Lists capped (~20–100 items) |
| No event edit/delete UI | Admin only |
| No batch approve/reject | One request at a time |
| No internal notes | All comments visible to requester |

### 13.3 Operations

| Gap | Notes |
|---|---|
| SQLite | Fine for dev; PostgreSQL recommended for production |
| No automated tests | Test suite not yet implemented |
| No CI/CD | Manual deploy |

### 13.4 Wise

| Gap | Notes |
|---|---|
| Sandbox by default | Production requires live API credentials and compliance review |
| No webhook reconciliation | Status polled manually |
| No exchange rate locking | Rate may change between approval and payment |

---

## 14. Roadmap

### Phase 1 — Production Hardening

| Task | Priority |
|---|---|
| Replace demo login with username/password on app routes | Critical |
| PostgreSQL support | High |
| Environment-based secrets (`SECRET_KEY`, `WISE_API_TOKEN`) | High |
| File upload validation (type, size) | High |
| Pagination on list views | Medium |
| Automated test suite | High |

### Phase 2 — Feature Completion

| Task | Priority |
|---|---|
| Email notifications (request state changes) | High |
| Event edit/delete in UI | Medium |
| Batch TSP approve/reject | Medium |
| Internal notes (TSP/Finance only) | Medium |
| Production Wise API + webhooks | High |

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
| `DJANGO_SECRET_KEY` | *(dev default)* | **Required for production** |
| `DATABASE_URL` | SQLite file | **Planned:** PostgreSQL connection |

## Appendix B: Management Commands

| Command | Description |
|---|---|
| `python manage.py migrate` | Apply database migrations |
| `python manage.py seed_data` | Create demo users, groups, and events |
| `python manage.py createsuperuser` | Create additional admin users |
| `python manage.py verify_wise_recipient` | CLI Wise recipient verification test |

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
