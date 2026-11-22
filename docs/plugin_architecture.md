# Dashboard with plugins (design, to be confirmed against the AIMS code)

Goal: one dashboard (the "host") in .NET, looking like AIMS (top bar, menu, summary cards,
filter bar, tables, dark/light mode). Each business area is a **plugin** that adds menu items,
pages and summary cards without changing the host:

- **Report plugin** (this project): search, report viewer, alerts, keywords, failed files, settings
- **FCR plugin**: the existing AIMS FCR register, moved in later
- more later, each one separate

This is a proposal. It must be checked against the real AIMS code (see the questions at the end).

## Who does what
| Part | Technology | Role |
|---|---|---|
| Importers, alert engine, backup | Python workers | Fill the database. Not plugins. They run as services/scheduled tasks |
| Database | PostgreSQL | Report data, search function `search_report_lines` |
| Host | ASP.NET Core | Login, roles, shared layout and theme, menu, plugin loading, home page with cards |
| Report plugin | .NET Razor Class Library | Pages that read the database (search function + EF models) |
| FCR plugin | .NET Razor Class Library | The existing FCR pages and its own database |

## Plugin contract (C#, proposal)
```csharp
public interface IDashboardPlugin
{
    string Id { get; }                         // "report", "fcr"
    string DisplayName { get; }
    IEnumerable<MenuGroup> Menu { get; }       // e.g. "Reports" > Search, Viewer, Alerts
    IEnumerable<SummaryCard> Cards(...);       // numbers for the home page (Total reports, Failed files)
    IEnumerable<Permission> Permissions { get; } // "report.view", "report.admin"
    void ConfigureServices(IServiceCollection services, IConfiguration config); // its DbContext, services
}
```
- A plugin is a **Razor Class Library**: its controllers/pages (area `/report/...`), views and
  static files travel inside the DLL (`_content/<PluginId>/...`).
- Each plugin owns its data and migrations. The host never reads a plugin's tables.
- The host builds the menu from all enabled plugins, filtered by the user's permissions.
- Enable/disable per plugin in Administration (a small `plugins` table).
- **Loading**: start with normal project references (simple, debuggable). Loading DLLs from a
  `plugins\` folder at startup (`AssemblyLoadContext` + application parts) can be added later.

## Look and feel
`search_engine/web/app.css` already defines the colour tokens for light and dark mode (variables
such as `--brand`, `--panel`, `--card-sel`). The host layout reuses the same tokens so every
plugin follows the theme. The preview page shows the pattern: top bar, menu, summary cards,
filter bar, table with sticky header, side drawer for one report.

## What the Report plugin calls
1. SQL function (single source of truth for search, same result in Python and .NET):
   `SELECT * FROM search_report_lines(query, mode, report_type, date_from, date_to, equipment, limit, offset)`
   `mode` is `fulltext` (whole words and tags) or `partial` (substrings and typos).
2. Normal EF Core entities for reports, report_lines, keyword_rules, alert_events, app_settings
   (scaffolded from the database; Python owns the schema until the host exists).
3. The JSON endpoints in `search_engine/api.py` document the shapes the pages need:
   `/api/search`, `/api/reports`, `/api/reports/{id}`, `/api/lines/{id}/context`, `/api/stats`,
   `/api/report-types`. Fields ending in `_html` are already escaped and contain only `<mark>`.
   Other text fields are plain data and must be HTML-encoded by the page (Razor does this by default).

## Moving FCR in later
1. Move the FCR controllers, views, models and its DbContext into a class library `...Plugins.Fcr`.
2. Replace its own layout with the host layout; keep its CSS only if needed.
3. Register its menu (Requests > FCR Register) and permissions in its plugin class.
4. Keep its database as is. No data migration is needed.

## Questions to confirm with the existing AIMS app
1. Which .NET version, and is it MVC, Razor Pages, Blazor, or an API plus a JavaScript front end?
2. How do users log in (Windows/AD, ASP.NET Identity, other) and how are roles stored?
3. Which database holds FCR (SQL Server?) and which ORM (EF Core, Dapper)?
4. Which CSS/JS framework styles the pages (Bootstrap, Tailwind, a template)?
5. Is the AIMS source available to copy the layout (`_Layout.cshtml`) as the host layout?
