# CI-4 frontend plan: the editor (draft, 2026-10-17)

Built against the contract in `docs/api.md` ("Your indicator definitions", PR #37). Design: `docs/custom-indicators-design.md` 5.1 and 5.2. One branch, one draft PR, no merge without the owner.

**Safety rules (from the security review), enforced in the client and tested**
- Plain files only: ids `[a-z][a-z0-9_]{0,39}`, whole string, lowercase. New and Duplicate lowercase what is typed and refuse the rest before any request; a name from Import is made by the server. Every path is built by `definitionPath` (encoded).
- No links: the editor renders source, errors, titles and tracebacks with `textContent` only. No markdown, no URL becomes clickable. A line link only moves the cursor.
- 256 KB cap (UTF-8 bytes, not characters): Save and Run are disabled above it with the size shown; a file picked for Import is refused above it before it is read into the page.
- Editing off (`catalog.editing.allowed` false): New, Import, Edit, Rename, Duplicate and Export are disabled with the server's reason shown once, in the panel. A `403` after the page thought editing was on turns it off the same way.

**The pieces**
- **Loading:** CodeMirror 6 (Python mode only) is a dynamic `import()` on the first Edit, New or Import click, never at page start. While it loads, and if the chunk fails, the panel uses a plain `<textarea>` with the same Save and Run. The licence goes into the third-party list. Bundle growth is measured and reported.
- **Menu:** each "Your files" row gets Edit, and a "..." menu with Duplicate, Rename, Export, Delete (delete exists, #36). New and Import sit in the section head. The picker model gains the actions as data, so they are tested without a browser.
- **Panel:** docked right on desktop so the chart stays visible, full screen on a phone (under 700 px), Escape closes, focus returns to the row. Ctrl+S saves, Ctrl+Enter runs, and nothing else is bound.
- **Save:** sends `base_version`. A `409` shows "changed elsewhere" with Reload (take the stored file) and Keep mine (save without `base_version`). Saved with errors is still saved: the error panel shows the problem. `unchanged` shows "No changes".
- **Run:** `POST /preview` on the chart's instrument, timeframe and price with the unsaved source. It draws as a dashed, labelled "preview" instance that is never on the chart's saved list and never saved to a layout. Closing the panel, switching chart or a new Run replaces or removes it. One Run at a time: a second click cancels the first.
- **Error panel:** type, message and traceback from a `422`, a load error from a save, or a worker `503`. "line N" moves the cursor and marks the line. Lookahead and warmup checks show as warnings.
- **Versions:** the list from `/versions` (newest first, at most 10). Opening one shows it read-only against the buffer; Restore asks first and is a save, so it can be undone.
- **Rename:** edits the title (1 to 60 characters, no line break). A `409` "edit TITLE in the code" is shown as is. The id never changes.
- **Duplicate, Export, Import:** Duplicate offers the suggested free id on a `409`. Export uses the download route. Import reads a `.py` file as text, offers its derived id, and never overwrites (`409` gives the suggestion).
- **Unsaved draft:** every edit is kept in `localStorage` per id with the `version` it started from, only as a convenience. The server's file is always the truth: the panel opens on the server's source, and offers the draft only if its base version still equals the stored one, "Restore draft" or "Discard". Storage that is blocked or full is ignored. Leaving with unsaved changes asks first.
- **Live updates:** `indicators_changed {id, op}` (from any tab) re-reads the catalog and the chart's list; if the open file changed elsewhere, the panel shows "changed elsewhere" and keeps the buffer.
- **Dead states:** no worker (`503`: save works, Run says why), file deleted while open (Save says the file is gone and offers Import of the buffer), server unreachable (buffer kept, retry), empty file (saved, shows the load error).

**Tests first, shown failing:** the pure parts (id and size checks, the draft rules, the save, run and conflict state machine, line links, picker actions) are in modules with no DOM, with a mock server for the `fetch` layer. Mutation check on those modules. Browser pass on a scratch server with the fake broker, both themes and a phone width.

**Not in this step:** a backend (Session B's); autocomplete beyond CodeMirror's defaults; a diff view for versions.
