# The AppContainer's own folder (CI-1, 2026-10-15)

What the indicator worker can do in the one folder an AppContainer may write: its own
(`%LOCALAPPDATA%\Packages\<container>\AC`, also its `TEMP`). Read this before changing how
that folder is handled. Script: [`appcontainer_own_folder.py`](appcontainer_own_folder.py),
run once as the owner's user (Windows 10), with a throwaway container
(`OpenChart.Investigation.<random>`), read access only on a throwaway Python copy, decoys
in a scratch folder, no network, and everything removed afterwards (cleanup proof: deny
entry removed, junction gone and decoy intact, profile deleted, 0 registry mappings, the
package folder gone, scratch deleted, 0 processes).

## Results

| Question | Result |
|---|---|
| Its own folder's ACL as created | the container's SID: full control `(OI)(CI)(F)`; SYSTEM, Administrators, the user: inherited full control; a Low mandatory label |
| Write, make a subfolder, delete | allowed |
| A size limit | none from Windows: 200 MB written at once (the script's own cap) |
| Files between two workers | they persist (the second worker found the first one's 207 files) |
| Files between scripts | one worker runs every script in one process: they share it |
| Its package folder, one level up | list and write refused |
| An outside folder, directly | refused |
| An outside folder through a junction the user made inside its folder | read and write refused |
| Making its own link | a symlink: refused (privilege); `mklink /J`: refused (the job's one-process limit) |
| **A deny entry for the container SID, `(OI)(CI)(DENY)(W,D,DC)`** | **written and listed first, but it does not hold: the worker still wrote, wrote in a subfolder and deleted** |

CI shows the same as LocalSystem: the service's worker could write there after the same
deny entry (its probe and notes; CI did not print that folder's ACL).

## What the server does since

The deny step is gone. The folder is treated as writable: emptied before every worker
start, and measured every 0.5 s while the worker lives; past 50 MB
(`OPENCHART_WORKER_FOLDER_MB`) the worker is killed (owner's decision B1,
`indicators/worker/runner.py`). The startup probe still writes there and its result is
logged and shown, with a note that says the folder is writable and shared by the
worker's scripts until the next start.

## Not tried

Removing the container's own full-control grant from that folder (instead of adding a
deny). It may stop what writes temporary files, and Windows may restore the grant when
the profile is opened again. A separate change, if ever (owner, 2026-10-15).
