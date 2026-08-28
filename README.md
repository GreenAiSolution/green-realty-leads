# Green Realty — Phoenix instant home-value lead funnel

Homeowner types an address → we pull the parcel from Maricopa County's open
assessor layer, price it against the last 12 months of real closed sales in the
ZIP, show the number → the comparable sales are behind name + phone + email.
That form is the lead. Leads land in SQLite on a Railway volume, on the
`/leads?key=…` dashboard (CSV export), and in your inbox if `RESEND_API_KEY` is set.

Pure Python standard library. `python3 app.py` runs it on :8195.

## Env
| var | default | what |
|---|---|---|
| `ADMIN_KEY` | (unset = dashboard off) | `/leads?key=ADMIN_KEY` |
| `DATA_DIR` | `./data` | mount a Railway volume at `/data` |
| `RESEND_API_KEY` | off | email every lead to `NOTIFY_EMAIL` |
| `NOTIFY_EMAIL` | jadengreen808@gmail.com | |
| `FROM_EMAIL` | leads@greenaidigital.com | must be a Resend-verified domain |
| `LICENSE_STATUS` | `pending` | flip to `licensed` when the AZ licence issues — changes the disclosure line |
| `SITE_NAME` | Green Realty | |

## Traffic
Append `?src=` to every link you put out so the dashboard tells you which
channel produced each lead: `?src=doorhanger-85032`, `?src=fb-nextdoor`,
`?src=discord`, `?src=truck-qr`.

## Compliance
- Disclosure footer states licensing status and that this is not an appraisal.
- TCPA consent checkbox with STOP language before any phone/text contact.
- Estimates come only from county public records (arm's-length filter ≥ $25k, 10% trimmed median $/sqft).
