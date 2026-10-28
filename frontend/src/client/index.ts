// Generado por `npm run generate` (scripts/generate.mjs) desde backend/openapi/app.yaml: no se edita a mano; `npm run generate:check` exige diferencia vacía (NFR-APP-48).

/**
 * Cliente de la plataforma (LC-APP-12): tipos de `openapi-typescript` y `openapi-fetch` sin
 * estado. Solo `src/ports/**` lo importa (BR-APP-62); tiempos de espera, cabeceras y errores son
 * de `ports.base`.
 */
import createClient, { type Client, type ClientOptions } from 'openapi-fetch';

import type { paths } from './schema';

/** Cliente tipado de las 151 operaciones de `app.yaml`; no guarda estado propio. */
export function createApiClient(options: ClientOptions = {}): Client<paths> {
  return createClient<paths>(options);
}

/** Operaciones de `app.yaml`: `operationId` → método y ruta, en el orden de la especificación. */
export const OPERATIONS = {
  live_health_live_get: { method: 'get', path: '/health/live' },
  checkpoint_keys__well_known_vigia_checkpoint_keys_get: {
    method: 'get',
    path: '/.well-known/vigia-checkpoint-keys',
  },
  verifier__well_known_vigia_verifier_get: { method: 'get', path: '/.well-known/vigia-verifier' },
  replay_platform_dead_letter__event_id___consumer__replay_post: {
    method: 'post',
    path: '/platform/dead-letter/{event_id}/{consumer}/replay',
  },
  rotate_platform_keys__purpose__rotate_post: {
    method: 'post',
    path: '/platform/keys/{purpose}/rotate',
  },
  login_auth_login_post: { method: 'post', path: '/auth/login' },
  second_factor_auth_second_factor_post: { method: 'post', path: '/auth/second-factor' },
  enroll_auth_second_factor_enroll_post: { method: 'post', path: '/auth/second-factor/enroll' },
  logout_auth_logout_post: { method: 'post', path: '/auth/logout' },
  sessions_auth_sessions_get: { method: 'get', path: '/auth/sessions' },
  close_others_auth_sessions_close_others_post: {
    method: 'post',
    path: '/auth/sessions/close-others',
  },
  change_password_auth_password_post: { method: 'post', path: '/auth/password' },
  accept_invitations__token__accept_post: { method: 'post', path: '/invitations/{token}/accept' },
  me_me_get: { method: 'get', path: '/me' },
  accept_notice_privacy_notice_accept_post: { method: 'post', path: '/privacy-notice/accept' },
  list_users_users_get: { method: 'get', path: '/users' },
  invite_users_post: { method: 'post', path: '/users' },
  deactivate_users__user_id__deactivate_post: {
    method: 'post',
    path: '/users/{user_id}/deactivate',
  },
  reactivate_users__user_id__reactivate_post: {
    method: 'post',
    path: '/users/{user_id}/reactivate',
  },
  update_profile_users__user_id__patch: { method: 'patch', path: '/users/{user_id}' },
  reset_second_factor_users__user_id__second_factor_reset_post: {
    method: 'post',
    path: '/users/{user_id}/second-factor/reset',
  },
  assign_role_users__user_id__roles_post: { method: 'post', path: '/users/{user_id}/roles' },
  remove_role_users__user_id__roles__assignment_id__delete: {
    method: 'delete',
    path: '/users/{user_id}/roles/{assignment_id}',
  },
  hierarchy_hierarchy_get: { method: 'get', path: '/hierarchy' },
  create_plant_plants_post: { method: 'post', path: '/plants' },
  create_zone_plants__plant_id__zones_post: { method: 'post', path: '/plants/{plant_id}/zones' },
  read_settings_organization_settings_get: { method: 'get', path: '/organization/settings' },
  update_settings_organization_settings_patch: { method: 'patch', path: '/organization/settings' },
  list_concessions_concessions_get: { method: 'get', path: '/concessions' },
  list_queries_concessions__concession_id__queries_get: {
    method: 'get',
    path: '/concessions/{concession_id}/queries',
  },
  revoke_concessions__concession_id__revoke_post: {
    method: 'post',
    path: '/concessions/{concession_id}/revoke',
  },
  own_concessions_provider_concessions_get: { method: 'get', path: '/provider/concessions' },
  grant_provider_concessions_post: { method: 'post', path: '/provider/concessions' },
  list_records_ledger_records_get: { method: 'get', path: '/ledger/records' },
  get_record_ledger_records__record_id__get: { method: 'get', path: '/ledger/records/{record_id}' },
  list_audit_entries_audit_entries_get: { method: 'get', path: '/audit/entries' },
  read_url_evidence__evidence_id__read_url_post: {
    method: 'post',
    path: '/evidence/{evidence_id}/read-url',
  },
  list_labels_labels_get: { method: 'get', path: '/labels' },
  timeline_zones__zone_id__coverage_get: { method: 'get', path: '/zones/{zone_id}/coverage' },
  status_at_zones__zone_id__coverage_at_get: {
    method: 'get',
    path: '/zones/{zone_id}/coverage/at',
  },
  verify_integrity_verify_post: { method: 'post', path: '/integrity/verify' },
  results_integrity_results_get: { method: 'get', path: '/integrity/results' },
  checkpoints_integrity_checkpoints_get: { method: 'get', path: '/integrity/checkpoints' },
  live_view_token_zones__zone_id__live_view_token_post: {
    method: 'post',
    path: '/zones/{zone_id}/live-view-token',
  },
  evaluate_plants__plant_id__admissions_post: {
    method: 'post',
    path: '/plants/{plant_id}/admissions',
  },
  list_admissions_plants__plant_id__admissions_get: {
    method: 'get',
    path: '/plants/{plant_id}/admissions',
  },
  grant_document_documents_post: { method: 'post', path: '/documents' },
  gate_state_zones__zone_id__gates_get: { method: 'get', path: '/zones/{zone_id}/gates' },
  scope_record_zones__zone_id__gates_mounting_scope_record_post: {
    method: 'post',
    path: '/zones/{zone_id}/gates/mounting/scope-record',
  },
  revocation_zones__zone_id__gates__gate__revocation_post: {
    method: 'post',
    path: '/zones/{zone_id}/gates/{gate}/revocation',
  },
  sign_policy_plants__plant_id__policy_post: { method: 'post', path: '/plants/{plant_id}/policy' },
  plant_policy_plants__plant_id__policy_get: { method: 'get', path: '/plants/{plant_id}/policy' },
  current_catalog_zones__zone_id__catalog_get: { method: 'get', path: '/zones/{zone_id}/catalog' },
  catalog_history_zones__zone_id__catalog_versions_get: {
    method: 'get',
    path: '/zones/{zone_id}/catalog/versions',
  },
  catalog_version_zones__zone_id__catalog_versions__catalog_version__get: {
    method: 'get',
    path: '/zones/{zone_id}/catalog/versions/{catalog_version}',
  },
  single_occupancy_zones__zone_id__catalog_single_occupancy_put: {
    method: 'put',
    path: '/zones/{zone_id}/catalog/single-occupancy',
  },
  new_standard_zones__zone_id__standards_post: {
    method: 'post',
    path: '/zones/{zone_id}/standards',
  },
  new_standard_version_zones__zone_id__standards__standard_id__versions_post: {
    method: 'post',
    path: '/zones/{zone_id}/standards/{standard_id}/versions',
  },
  retirement_zones__zone_id__standards__standard_id__retirement_post: {
    method: 'post',
    path: '/zones/{zone_id}/standards/{standard_id}/retirement',
  },
  cameras_zones__zone_id__cameras_put: { method: 'put', path: '/zones/{zone_id}/cameras' },
  minimum_coverage_zones__zone_id__minimum_coverage_put: {
    method: 'put',
    path: '/zones/{zone_id}/minimum-coverage',
  },
  signals_zones__zone_id__signals_put: { method: 'put', path: '/zones/{zone_id}/signals' },
  thresholds_zones__zone_id__thresholds_put: { method: 'put', path: '/zones/{zone_id}/thresholds' },
  windows_zones__zone_id__windows_put: { method: 'put', path: '/zones/{zone_id}/windows' },
  regression_state_zones__zone_id__regression_get: {
    method: 'get',
    path: '/zones/{zone_id}/regression',
  },
  framing_recapture_zones__zone_id__framing_recaptures_post: {
    method: 'post',
    path: '/zones/{zone_id}/framing-recaptures',
  },
  put_signatory_policy_plants__plant_id__signatory_policy_put: {
    method: 'put',
    path: '/plants/{plant_id}/signatory-policy',
  },
  signatory_policy_plants__plant_id__signatory_policy_get: {
    method: 'get',
    path: '/plants/{plant_id}/signatory-policy',
  },
  create_agreement_zones__zone_id__use_agreements_post: {
    method: 'post',
    path: '/zones/{zone_id}/use-agreements',
  },
  confirm_agreement_use_agreements__agreement_id__confirmations_post: {
    method: 'post',
    path: '/use-agreements/{agreement_id}/confirmations',
  },
  approve_agreement_use_agreements__agreement_id__approval_post: {
    method: 'post',
    path: '/use-agreements/{agreement_id}/approval',
  },
  transparency_zones__zone_id__transparency_get: {
    method: 'get',
    path: '/zones/{zone_id}/transparency',
  },
  open_walk_test_zones__zone_id__walk_tests_post: {
    method: 'post',
    path: '/zones/{zone_id}/walk-tests',
  },
  current_walk_test_zones__zone_id__walk_tests_current_get: {
    method: 'get',
    path: '/zones/{zone_id}/walk-tests/current',
  },
  start_step_walk_tests__session_id__steps_post: {
    method: 'post',
    path: '/walk-tests/{session_id}/steps',
  },
  close_step_walk_tests__session_id__steps__step_id__close_post: {
    method: 'post',
    path: '/walk-tests/{session_id}/steps/{step_id}/close',
  },
  record_pass_walk_tests__session_id__passes_post: {
    method: 'post',
    path: '/walk-tests/{session_id}/passes',
  },
  reopen_walk_test_walk_tests__session_id__reopen_post: {
    method: 'post',
    path: '/walk-tests/{session_id}/reopen',
  },
  record_occlusion_test_walk_tests__session_id__occlusion_tests_post: {
    method: 'post',
    path: '/walk-tests/{session_id}/occlusion-tests',
  },
  open_regression_rerun_zones__zone_id__walk_tests_regression_rerun_post: {
    method: 'post',
    path: '/zones/{zone_id}/walk-tests/regression-rerun',
  },
  close_walk_test_walk_tests__session_id__close_post: {
    method: 'post',
    path: '/walk-tests/{session_id}/close',
  },
  record_exposure_sample_walk_tests__session_id__exposure_samples_post: {
    method: 'post',
    path: '/walk-tests/{session_id}/exposure-samples',
  },
  commissioning_record_commissioning_records__record_id__get: {
    method: 'get',
    path: '/commissioning-records/{record_id}',
  },
  commissioning_record_document_commissioning_records__record_id__document_get: {
    method: 'get',
    path: '/commissioning-records/{record_id}/document',
  },
  declare_node_plants__plant_id__nodes_post: { method: 'post', path: '/plants/{plant_id}/nodes' },
  assign_zone_nodes__node_id__zones_post: { method: 'post', path: '/nodes/{node_id}/zones' },
  unassign_zone_nodes__node_id__zones__zone_id__unassignment_post: {
    method: 'post',
    path: '/nodes/{node_id}/zones/{zone_id}/unassignment',
  },
  issue_enrollment_code_nodes__node_id__enrollment_codes_post: {
    method: 'post',
    path: '/nodes/{node_id}/enrollment-codes',
  },
  list_enrollment_attempts_nodes__node_id__enrollment_attempts_get: {
    method: 'get',
    path: '/nodes/{node_id}/enrollment-attempts',
  },
  revoke_node_nodes__node_id__revocation_post: {
    method: 'post',
    path: '/nodes/{node_id}/revocation',
  },
  decommission_node_nodes__node_id__decommission_post: {
    method: 'post',
    path: '/nodes/{node_id}/decommission',
  },
  commissioning_clips_zones__zone_id__commissioning_clips_get: {
    method: 'get',
    path: '/zones/{zone_id}/commissioning-clips',
  },
  list_fleet_nodes_fleet_nodes_get: { method: 'get', path: '/fleet/nodes' },
  fleet_node_fleet_nodes__node_id__get: { method: 'get', path: '/fleet/nodes/{node_id}' },
  plant_fleet_thresholds_plants__plant_id__fleet_thresholds_get: {
    method: 'get',
    path: '/plants/{plant_id}/fleet-thresholds',
  },
  put_plant_fleet_thresholds_plants__plant_id__fleet_thresholds_put: {
    method: 'put',
    path: '/plants/{plant_id}/fleet-thresholds',
  },
  publish_target_version_fleet_target_versions_post: {
    method: 'post',
    path: '/fleet/target-versions',
  },
  list_fleet_alarms_fleet_alarms_get: { method: 'get', path: '/fleet/alarms' },
  post_clip_upload: { method: 'post', path: '/api/nodes/clip-uploads' },
  post_clip_upload_confirmation: {
    method: 'post',
    path: '/api/nodes/clip-uploads/{clip_id}/confirmation',
  },
  post_credential_rotation: { method: 'post', path: '/api/nodes/credential-rotations' },
  post_detection_review: { method: 'post', path: '/api/nodes/detection-reviews' },
  post_enrollment: { method: 'post', path: '/api/nodes/enrollment' },
  post_finding: { method: 'post', path: '/api/nodes/findings' },
  post_heartbeat: { method: 'post', path: '/api/nodes/heartbeats' },
  post_observability_event: { method: 'post', path: '/api/nodes/observability-events' },
  post_update_result: { method: 'post', path: '/api/nodes/update-results' },
  get_zone_catalog: { method: 'get', path: '/api/nodes/zones/{zone_id}/catalog' },
  upload_grant_loop_actions__action_id__attachments_upload_grant_post: {
    method: 'post',
    path: '/loop/actions/{action_id}/attachments/upload-grant',
  },
  read_url_loop_attachments__attachment_id__read_url_post: {
    method: 'post',
    path: '/loop/attachments/{attachment_id}/read-url',
  },
  configuration_loop_plants__plant_id__configuration_get: {
    method: 'get',
    path: '/loop/plants/{plant_id}/configuration',
  },
  change_configuration_loop_plants__plant_id__configuration_put: {
    method: 'put',
    path: '/loop/plants/{plant_id}/configuration',
  },
  classify_loop_findings__finding_state_id__classifications_post: {
    method: 'post',
    path: '/loop/findings/{finding_state_id}/classifications',
  },
  list_classifications_loop_findings__finding_state_id__classifications_get: {
    method: 'get',
    path: '/loop/findings/{finding_state_id}/classifications',
  },
  list_review_queue_loop_review_queue_get: { method: 'get', path: '/loop/review-queue' },
  resolve_loop_review_queue__detection_id__resolution_post: {
    method: 'post',
    path: '/loop/review-queue/{detection_id}/resolution',
  },
  assign_loop_findings__finding_state_id__actions_post: {
    method: 'post',
    path: '/loop/findings/{finding_state_id}/actions',
  },
  add_evidence_loop_actions__action_id__evidence_post: {
    method: 'post',
    path: '/loop/actions/{action_id}/evidence',
  },
  close_loop_findings__finding_state_id__closure_post: {
    method: 'post',
    path: '/loop/findings/{finding_state_id}/closure',
  },
  reopen_loop_findings__finding_state_id__reopening_post: {
    method: 'post',
    path: '/loop/findings/{finding_state_id}/reopening',
  },
  start_loop_findings__finding_state_id__root_cause_analyses_post: {
    method: 'post',
    path: '/loop/findings/{finding_state_id}/root-cause-analyses',
  },
  in_progress_loop_findings__finding_state_id__root_cause_analyses_in_progress_get: {
    method: 'get',
    path: '/loop/findings/{finding_state_id}/root-cause-analyses/in-progress',
  },
  put_step_loop_root_cause_analyses__analysis_id__steps__step_index__put: {
    method: 'put',
    path: '/loop/root-cause-analyses/{analysis_id}/steps/{step_index}',
  },
  signature_loop_root_cause_analyses__analysis_id__signature_post: {
    method: 'post',
    path: '/loop/root-cause-analyses/{analysis_id}/signature',
  },
  request_assistant_loop_root_cause_analyses__analysis_id__assistant_requests_post: {
    method: 'post',
    path: '/loop/root-cause-analyses/{analysis_id}/assistant-requests',
  },
  assistant_status_loop_assistant_requests__request_id__get: {
    method: 'get',
    path: '/loop/assistant-requests/{request_id}',
  },
  list_findings_loop_findings_get: { method: 'get', path: '/loop/findings' },
  get_finding_loop_findings__finding_state_id__get: {
    method: 'get',
    path: '/loop/findings/{finding_state_id}',
  },
  list_overdue_loop_overdue_get: { method: 'get', path: '/loop/overdue' },
  vocabulary_loop_vocabulary_get: { method: 'get', path: '/loop/vocabulary' },
  change_vocabulary_loop_vocabulary_changes_post: {
    method: 'post',
    path: '/loop/vocabulary/changes',
  },
  declare_loop_findings__finding_state_id__unclosable_post: {
    method: 'post',
    path: '/loop/findings/{finding_state_id}/unclosable',
  },
  review_loop_unclosable__declaration_id__reviews_post: {
    method: 'post',
    path: '/loop/unclosable/{declaration_id}/reviews',
  },
  request_export_accreditation_exports_post: { method: 'post', path: '/accreditation/exports' },
  export_status_accreditation_exports__export_id__get: {
    method: 'get',
    path: '/accreditation/exports/{export_id}',
  },
  export_download_accreditation_exports__export_id__download_get: {
    method: 'get',
    path: '/accreditation/exports/{export_id}/download',
  },
  export_document_url_accreditation_exports__export_id__download_url_post: {
    method: 'post',
    path: '/accreditation/exports/{export_id}/download-url',
  },
  get_monthly_package_accreditation_monthly_packages_get: {
    method: 'get',
    path: '/accreditation/monthly-packages',
  },
  get_monthly_package_sample_accreditation_monthly_packages__package_id__sample_get: {
    method: 'get',
    path: '/accreditation/monthly-packages/{package_id}/sample',
  },
  get_monthly_package_plant_download_accreditation_monthly_packages__plant_id___period__download_get:
    { method: 'get', path: '/accreditation/monthly-packages/{plant_id}/{period}/download' },
  record_sample_result_accreditation_sample_items__sample_item_id__result_post: {
    method: 'post',
    path: '/accreditation/sample-items/{sample_item_id}/result',
  },
  record_commitments_accreditation_monthly_packages__package_id__commitments_post: {
    method: 'post',
    path: '/accreditation/monthly-packages/{package_id}/commitments',
  },
  update_commitment_accreditation_commitments__commitment_id__patch: {
    method: 'patch',
    path: '/accreditation/commitments/{commitment_id}',
  },
  list_commitments_accreditation_commitments_get: {
    method: 'get',
    path: '/accreditation/commitments',
  },
  get_exposure_accreditation_exposure_get: { method: 'get', path: '/accreditation/exposure' },
  get_adoption_accreditation_adoption_get: { method: 'get', path: '/accreditation/adoption' },
  metrics_accreditation_metrics_get: { method: 'get', path: '/accreditation/metrics' },
  list_notifications_notifications_get: { method: 'get', path: '/notifications' },
  mark_notification_read_notifications__notification_id__read_post: {
    method: 'post',
    path: '/notifications/{notification_id}/read',
  },
  read_preferences_notifications_preferences_get: {
    method: 'get',
    path: '/notifications/preferences',
  },
  update_preferences_notifications_preferences_put: {
    method: 'put',
    path: '/notifications/preferences',
  },
} as const satisfies Readonly<
  Record<string, { readonly method: string; readonly path: keyof paths }>
>;
