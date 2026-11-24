# Master Plan v4: Dashboard Platform with Plugins

Update of Master Plan v3. The Report Intelligence work (Phases 0 to 6) is unchanged. What is new is the **big picture**: Report Intelligence is the first plugin of a larger dashboard platform that will later also run FCR as a paperless process with departments, roles, approvals and signatures. This document keeps that whole picture in one place so nothing built now has to be redone later.

## 1. The big picture

```
                     MAIN DASHBOARD (host, .NET, AIMS look and feel)
  +-----------------------------------------------------------------------+
  |  PLATFORM CORE (shared by every plugin)                               |
  |   Login (Windows/AD)  |  Departments, roles, permissions              |
  |   Workflow engine: approvals + signatures  |  Audit log               |
  |   Notifications       |  Plugin registry + per-plugin settings/flags  |
  |   Layout, menu, dark/light theme, home page cards                     |
  +-----------------------------------------------------------------------+
        |                         |                          |
   REPORT plugin             FCR plugin                 future plugins
   (workflow OFF)            (workflow ON)
        |
   PostgreSQL <--- Python workers (Outlook import, historical import, alerts, backup)
```

- **Platform core** owns people, departments, roles, permissions, workflows, signatures, audit and notifications.
- **Plugins** own their own data and pages. They ask the core for login, permissions and approvals instead of building their own.
- **Python workers** are background services, not plugins. They fill the Report database.

## 2. What each plugin switches on

Every plugin declares which platform features it uses. Administration shows one switch per feature, per plugin.

| Feature | Report plugin | FCR plugin |
| --- | --- | --- |
| Login and permissions | on | on |
| Departments and roles | on (who may view, who may manage keywords) | on |
| Approval workflow | **off** | **on** |
| Signatures | **off** | **on** |
| Audit log | basic (settings changes, alert acknowledgements) | full |
| Notifications | keyword alerts | approval requests |
| Own settings page | on | on |

When a feature is off, the plugin shows no approval or signature screens and the core never asks it for them.

## 3. Approval and signature system (outline only, designed later)

Not the focus now. Only the shape is fixed here so earlier phases leave room for it.

- **Organisation:** departments, roles, users with one or more role-in-department assignments, substitutes when someone is away.
- **Workflow definition per plugin item type** (for example "FCR"): ordered steps, each step assigned to a role or department, optional parallel steps and conditions (for example cost above a limit adds a step).
- **Workflow instance** attached to one plugin document (a reference such as `fcr:1234`). The plugin keeps the document data; the core keeps the workflow state.
- **Signature record:** who, which role, when, which version of the document, which decision (approve, reject, return). The signing method (authenticated click, PIN, digital certificate) is decided with your QA and legal people before building.
- **Document lock and snapshot:** after final approval the document is locked and a PDF snapshot with the signature block is stored.
- **Audit trail:** append-only, nothing is deleted.
- **Core API for plugins (idea):** `StartWorkflow(documentRef, definition)`, `GetState(documentRef)`, events `Approved`, `Rejected`, `Returned`.
- **Notifications:** email and Teams to whoever has a pending step.

## 4. Rules to keep now (cheap now, expensive later)

1. **Permissions, not fixed roles.** Code checks permissions such as `report.view` or `fcr.approve`. Roles are just named bundles of permissions edited in Administration.
2. **Each plugin owns its data.** No plugin reads another plugin's tables. They talk through the core.
3. **Own database area per plugin.** Move the Report tables into their own PostgreSQL schema (for example `report`) while the database is still empty, so generic names like `app_settings` and `job_runs` never clash with the core or FCR. This is one small migration now.
4. **Identity is the Windows/AD account.** Plugins store the user key (account name), not their own user tables.
5. **No hard deletes of business data.** Use status flags so audit and approvals stay valid.
6. **One layout and one theme.** Colour tokens for light and dark mode live in the host; plugins only use them.
7. **Settings per plugin.** The Report plugin keeps its own settings (folders, intervals, keywords). Platform settings (departments, roles, workflows) live in the core.
8. **Settings that depend on a feature disappear when it is off.**

## 5. Requirements

| # | Requirement | Status |
| --- | --- | --- |
| R1 | Import historical Excel reports from 2013 onward, any format and layout | engine done, real layouts pending |
| R2 | Import new reports automatically from a dedicated Outlook folder | done (Phase 5), needs your Outlook access setup |
| R3 | 12H, biweekly, monthly reports; easy to add types | engine done |
| R4 | Keyword search across all reports | done (Phase 4) |
| R5 | Keyword notifications for new reports only | done (Phase 6) |
| R6 | Dashboard: choose report type, every report line in its own row | preview done, final UI in Phase 8 |
| R7 | Settings screen (folders, intervals, keywords, backup) | Phase 8 |
| R8 | Dark and light mode | preview done |
| R9 | Plug into the company .NET dashboard | Phase 7 and 8 |
| R10 | Runs internally without Docker | yes |
| R11 | Alerts can be acknowledged with a note | command line done (Phase 6), screen in Phase 8 |
| R12 | Dashboard host with plugins (Report, FCR, more later) | Phase 7 |
| R13 | Departments, roles and permissions defined in the host | Phase 10 |
| R14 | Approvals and signatures for plugins that need them (FCR yes, Report no) | Phase 11 |
| R15 | FCR becomes paperless as a plugin | Phase 12 |

## 6. Roadmap

### Part A: Report Intelligence backend (Python, PostgreSQL)

| Phase | What | Status |
| --- | --- | --- |
| 0 | Discovery: inventory script to find the real layouts | script done, **needs to be run on your archive** |
| 1 | Foundation: database, settings, Windows setup scripts | done |
| 2 | Parser engine for .xls, .xlsx, .xlsm, .xlsb and the template system | done, real templates wait for Phase 0 results |
| 3 | Historical importer with dry run, resume, failed queue | done |
| 4 | Keyword search (database function, CLI, API, preview page) | done |
| 5 | Outlook live importer (Graph, desktop Outlook, or a folder of .eml files) | done, not yet tried against your real mailbox |
| 6 | Keyword alerts, missing-report detection, daily digest, action notes | done, not yet tried with your real SMTP server |

Details of Phases 0 to 6 are as in Master Plan v3 (rules: alerts only for new Outlook reports, idempotent imports by file hash, nothing silently dropped, secrets in environment files, dates from report content).

### Part B: Dashboard platform (.NET)

| Phase | What |
| --- | --- |
| 7 | **Host dashboard:** ASP.NET Core, AIMS layout, Windows login, menu built from plugins, theme, home page cards, plugin registry with per-plugin settings and feature switches, basic roles (admin, user) |
| 8 | **Report plugin:** Search, Reports viewer (type selector, one row per line), Alerts with acknowledge and note, Keywords, Failed files, Settings (including the interval and folder settings), status panel. Reads the database through the search function and EF models |
| 9 | **Backup, scheduling, deployment without Docker:** Windows services for workers and host, backup service, logging, restore guide |

The preview page in Phase 4 is only the visual draft of Phase 8. It is replaced, not extended.

### Part C: Organisation, approvals and FCR (outline, detailed when Part B works)

| Phase | What |
| --- | --- |
| 10 | **Departments, roles, permissions** in Administration. Replaces the basic roles of Phase 7. Optional import of department data from AD |
| 11 | **Workflow and signature engine** in the core: definitions, instances, signatures, delegation, audit, notifications, PDF snapshot |
| 12 | **FCR plugin:** move the existing AIMS FCR pages into a plugin, switch workflow and signatures on, define the FCR approval chain per department |
| 13 | **Hardening:** audit review, retention rules, backups for the whole platform, security review |

## 7. Open decisions (answer when Part B starts)

1. AIMS technology: .NET version, MVC / Razor Pages / Blazor, login method, FCR database and ORM, CSS framework. Share `_Layout.cshtml` if possible.
2. Repositories: one repository for host plus FCR (the AIMS repo) and a separate one for Report Intelligence, or everything together.
3. Signature level for FCR (simple authenticated approval or something stronger) decided with QA and legal.
4. Outlook access for Phase 5: Microsoft Graph (needs IT app registration) or desktop Outlook on a PC that stays logged in.
5. Where the server runs and whether it stays on 24/7.

## 8. What changed from v3

- Added the platform picture, the plugin feature table and Parts B and C.
- Old Phase 7 (separate .NET API) and Phase 8 (React UI) are replaced by the host (Phase 7) and the Report plugin (Phase 8).
- Old Phase 9 (deployment) stays as Phase 9.
- Added the rule to move Report tables into their own schema before real data is loaded.
- Phases 0 to 6, the data model and the Report rules stay as in v3.
