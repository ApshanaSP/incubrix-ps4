# Test pack index

Built 2026-10-17 23:46 by `python -m tools.make_test_pack`, in simulation mode.

| Claim | Evidence |
|---|---|
| All automated tests pass | [tests/pytest.txt](tests/pytest.txt), [tests/pytest.xml](tests/pytest.xml) |
| Every hard case and feature, run end to end over HTTP | the batch table below; [server.log](server.log) is the service's JSON log for the whole run |
| Certification: publish, live URL, status, metrics, delete, confirm deletion | [certifications.json](certifications.json) and 59 evidence files in [certification_evidence/](certification_evidence/) |
| Measured numbers (REPORT.md section 4) | [metrics/metrics.json](metrics/metrics.json), [metrics/recommender.json](metrics/recommender.json) |
| No secrets in logs | search [server.log](server.log) for the credential values in `tools/make_batch_inputs.py`: none appear; the reviewer check stores a random token and searches this log for it |
| Independent reviewer-style checks (every must-have, Integration point and hard case, other inputs than the batch) | 38/38 passed: [reviewer_check/20261017-234621/reviewer_check.json](reviewer_check/20261017-234621/reviewer_check.json) |
| Certification pack: run record, evidence page and screenshot per connector, real Lemmy and Ghost posts | [../certification_pack/INDEX.md](../certification_pack/INDEX.md) |
| Online catalogue import (real catalogue and 1,000 rows, API latency during the import) | [metrics/catalogue_import_online.json](metrics/catalogue_import_online.json) |
| Failed attempts, kept | first reviewer check (32/37 before fixes): [../outputs/reviewer_check/20261016-234023/](../outputs/reviewer_check/20261016-234023/reviewer_check.json); first compose batch run that hit the API's own rate limit: [../outputs/compose/batch-20261016-202108/](../outputs/compose/batch-20261016-202108/); REPORT.md section 3 |
| Real catalogue (150+ destinations), rules evidence | [../catalogue/real_catalogue.csv](../catalogue/real_catalogue.csv), [../catalogue/real_catalogue_evidence.json](../catalogue/real_catalogue_evidence.json) |

## Batch runs

| Input | Result | Output | Input file |
|---|---|---|---|
| `01_certify_and_publish_every_connector` | ok | [result](batch/batch-20261017-234545/01_certify_and_publish_every_connector.json) | [input](inputs/01_certify_and_publish_every_connector.json) |
| `02_timeout_then_retry_same_key` | ok | [result](batch/batch-20261017-234545/02_timeout_then_retry_same_key.json) | [input](inputs/02_timeout_then_retry_same_key.json) |
| `03_rate_limit_429_with_reset` | ok | [result](batch/batch-20261017-234545/03_rate_limit_429_with_reset.json) | [input](inputs/03_rate_limit_429_with_reset.json) |
| `04_rate_limit_short_backoff` | ok | [result](batch/batch-20261017-234545/04_rate_limit_short_backoff.json) | [input](inputs/04_rate_limit_short_backoff.json) |
| `05_expired_token` | ok | [result](batch/batch-20261017-234545/05_expired_token.json) | [input](inputs/05_expired_token.json) |
| `06_content_rejected` | ok | [result](batch/batch-20261017-234545/06_content_rejected.json) | [input](inputs/06_content_rejected.json) |
| `07_too_long_title_and_body` | ok | [result](batch/batch-20261017-234545/07_too_long_title_and_body.json) | [input](inputs/07_too_long_title_and_body.json) |
| `08_unsupported_media` | ok | [result](batch/batch-20261017-234545/08_unsupported_media.json) | [input](inputs/08_unsupported_media.json) |
| `09_second_post_inside_weekly_cap` | ok | [result](batch/batch-20261017-234545/09_second_post_inside_weekly_cap.json) | [input](inputs/09_second_post_inside_weekly_cap.json) |
| `10_promo_to_no_promotion_community` | ok | [result](batch/batch-20261017-234545/10_promo_to_no_promotion_community.json) | [input](inputs/10_promo_to_no_promotion_community.json) |
| `11_renamed_api_field` | ok | [result](batch/batch-20261017-234545/11_renamed_api_field.json) | [input](inputs/11_renamed_api_field.json) |
| `12_csv_duplicates_bad_urls_mixed_languages` | ok | [result](batch/batch-20261017-234545/12_csv_duplicates_bad_urls_mixed_languages.json) | [input](inputs/12_csv_duplicates_bad_urls_mixed_languages.json) |
| `13_partner_revokes_permission` | ok | [result](batch/batch-20261017-234545/13_partner_revokes_permission.json) | [input](inputs/13_partner_revokes_permission.json) |
| `14_non_english_community` | ok | [result](batch/batch-20261017-234545/14_non_english_community.json) | [input](inputs/14_non_english_community.json) |
| `15_ai_disclosure_required` | ok | [result](batch/batch-20261017-234545/15_ai_disclosure_required.json) | [input](inputs/15_ai_disclosure_required.json) |
| `16_rules_capture_needs_confirmation` | ok | [result](batch/batch-20261017-234545/16_rules_capture_needs_confirmation.json) | [input](inputs/16_rules_capture_needs_confirmation.json) |
| `17_recommend_destinations` | ok | [result](batch/batch-20261017-234545/17_recommend_destinations.json) | [input](inputs/17_recommend_destinations.json) |
