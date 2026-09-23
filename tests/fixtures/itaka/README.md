# ITAKA offline evidence

These four JSON fixtures are minimized observations of public listing and selected
detail HTML retrieved on 2026-09-12, for FUERIOC, RMFTULR, SIDROYA and CFUANGE.
No requests are made when generating test HTML or running tests.

Retained: listing variant and price fields, selected detail variant, flight summary,
and relevant practical information. Removed: photos, customer reviews, analytics,
scripts, marketing content and unrelated alternative offers. Public rate IDs and
the pre-enrichment identity hashes are retained to protect history compatibility.
The dates of birth are the public site's default adult placeholders, not customer data.

The detail objects were extracted from embedded Flight records, resolving their
references. Tests reframe these objects into split Flight chunks, including JSON
references and UTF-8 byte-counted text. Additional decoder tests exercise malformed
framing and references. Full saved responses were also checked locally during
implementation; they are not committed because they contain substantial unrelated content.

Monetary mutations used in eligibility tests are explicitly synthetic. The original
booking totals are 7058, 5858, 9358 and 3678 PLN respectively. Neither these samples
nor offline checks establish current availability or future rate-ID stability.

`FUERIOC_selection.json` is separate minimized evidence from the approved September 20
manual capture. It retains the listing, the expected rate's participant-group fields
from `initialParticipantGroups` and `current.participantGroups`, and a different full
default variant. Unrelated detail fields are omitted; the source participant models
retain the site's public default birth-date placeholders. The expected
groups do not provide all fields required by `Variant`; selection must find their exact
ID and fail closed on missing evidence, never borrow fields from the default variant.
Synthetic two-variant tests separately verify successful confirmation in either order
when complete evidence for the expected rate is present.

`FUERIOC_mapping.json` comes from the subsequent final September 20 capture. It retains
rate-linked metadata, participant groups, their date/price context, room/meal aliases,
the unrelated default variant and the mixed summary. `source_paths` records the original
Flight record paths. Photos, descriptions and participant birth-date details are removed.
Seven missing fields can be mapped from this evidence. No rateType or complete transport
is available for the expected rate: the summary has its offer ID but the default variant's
Warsaw/November flights. Tests must leave it unconfirmed rather than infer these fields.
