// Generado por `npm run generate` (scripts/generate.mjs) desde backend/openapi/app.yaml: no se edita a mano; `npm run generate:check` exige diferencia vacía (NFR-APP-48).
/* eslint-disable @typescript-eslint/consistent-indexed-object-style -- forma que emite openapi-typescript. */
export interface paths {
  '/health/live': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Salud superficial: el proceso atiende */
    get: operations['live_health_live_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/.well-known/vigia-checkpoint-keys': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Claves públicas de los puntos de control, también las retiradas (público) */
    get: operations['checkpoint_keys__well_known_vigia_checkpoint_keys_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/.well-known/vigia-verifier': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** SHA-256 del verificador de paquetes vigia_verify.py (público) */
    get: operations['verifier__well_known_vigia_verifier_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/platform/dead-letter/{event_id}/{consumer}/replay': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Reentrega desde la cola muerta con el mismo event_id (solo el operador) */
    post: operations['replay_platform_dead_letter__event_id___consumer__replay_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/platform/keys/{purpose}/rotate': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Rota la clave de firma de un propósito (solo el operador) */
    post: operations['rotate_platform_keys__purpose__rotate_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/auth/login': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Inicio de sesión: primer paso (correo y contraseña) */
    post: operations['login_auth_login_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/auth/second-factor': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Inicio de sesión: segundo paso (TOTP o código de recuperación) */
    post: operations['second_factor_auth_second_factor_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/auth/second-factor/enroll': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Inscripción obligatoria del segundo factor con la sesión pendiente */
    post: operations['enroll_auth_second_factor_enroll_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/auth/logout': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Cierre de sesión */
    post: operations['logout_auth_logout_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/auth/sessions': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Sesiones activas de la persona */
    get: operations['sessions_auth_sessions_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/auth/sessions/close-others': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Cerrar las demás sesiones */
    post: operations['close_others_auth_sessions_close_others_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/auth/password': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Cambio de la contraseña propia */
    post: operations['change_password_auth_password_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/invitations/{token}/accept': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Activación de la cuenta con el enlace de invitación */
    post: operations['accept_invitations__token__accept_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/me': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** La persona de la sesión: identidad, asignaciones, permisos y vencimientos */
    get: operations['me_me_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/privacy-notice/accept': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Aceptación de la versión vigente del aviso de tratamiento */
    post: operations['accept_notice_privacy_notice_accept_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/users': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Cuentas de la organización con sus asignaciones vigentes */
    get: operations['list_users_users_get'];
    put?: never;
    /** Invitar a una persona con al menos una asignación */
    post: operations['invite_users_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/users/{user_id}/deactivate': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Desactivar una cuenta (nunca se borra) */
    post: operations['deactivate_users__user_id__deactivate_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/users/{user_id}/reactivate': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Reactivar una cuenta con asignaciones nuevas y otra invitación */
    post: operations['reactivate_users__user_id__reactivate_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/users/{user_id}': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    /** Editar el nombre y la licencia profesional */
    patch: operations['update_profile_users__user_id__patch'];
    trace?: never;
  };
  '/users/{user_id}/second-factor/reset': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Restablecer el segundo factor de una persona */
    post: operations['reset_second_factor_users__user_id__second_factor_reset_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/users/{user_id}/roles': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Asignar un rol sobre un alcance */
    post: operations['assign_role_users__user_id__roles_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/users/{user_id}/roles/{assignment_id}': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post?: never;
    /** Retirar una asignación (queda con su fecha) */
    delete: operations['remove_role_users__user_id__roles__assignment_id__delete'];
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/hierarchy': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Organización, plantas, zonas y nodos al alcance de la sesión */
    get: operations['hierarchy_hierarchy_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/plants': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Crear una planta con la génesis de su cadena */
    post: operations['create_plant_plants_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/plants/{plant_id}/zones': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Crear una zona de la planta */
    post: operations['create_zone_plants__plant_id__zones_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/organization/settings': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Datos y topes de concesión de la organización */
    get: operations['read_settings_organization_settings_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    /** Cambiar el nombre y los topes de concesión */
    patch: operations['update_settings_organization_settings_patch'];
    trace?: never;
  };
  '/concessions': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Concesiones del proveedor sobre la organización (cualquier estado) */
    get: operations['list_concessions_concessions_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/concessions/{concession_id}/queries': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Consultas del proveedor bajo una concesión, en orden de llegada */
    get: operations['list_queries_concessions__concession_id__queries_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/concessions/{concession_id}/revoke': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Revocar una concesión de inmediato */
    post: operations['revoke_concessions__concession_id__revoke_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/provider/concessions': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Concesiones que la persona del proveedor se concedió */
    get: operations['own_concessions_provider_concessions_get'];
    put?: never;
    /** Concederse acceso temporal a una organización cliente */
    post: operations['grant_provider_concessions_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/ledger/records': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Registros del expediente con alcance, paginados por clave (lectura auditada) */
    get: operations['list_records_ledger_records_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/ledger/records/{record_id}': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Un registro del expediente (lectura auditada); fuera de alcance, not_found */
    get: operations['get_record_ledger_records__record_id__get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/audit/entries': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Entradas de la cadena de auditoría con alcance (lectura auditada) */
    get: operations['list_audit_entries_audit_entries_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/evidence/{evidence_id}/read-url': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** URL de solo lectura de un clip, vigente 5 minutos (concesión auditada) */
    post: operations['read_url_evidence__evidence_id__read_url_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/labels': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Etiquetas por periodo, zona, familia y categoría de motivo (consulta auditada) */
    get: operations['list_labels_labels_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/coverage': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Línea de tiempo de cobertura de la zona en [from, to), tope de 31 días */
    get: operations['timeline_zones__zone_id__coverage_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/coverage/at': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Estado compuesto de cobertura de la zona en un instante */
    get: operations['status_at_zones__zone_id__coverage_at_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/integrity/verify': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Pide verificar una cadena a demanda; la verificación corre en el worker */
    post: operations['verify_integrity_verify_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/integrity/results': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Último resultado de la verificación de cada cadena a su alcance */
    get: operations['results_integrity_results_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/integrity/checkpoints': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Último punto de control firmado de cada cadena a su alcance */
    get: operations['checkpoints_integrity_checkpoints_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/live-view-token': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Token de 10 minutos para la vista en vivo difuminada de la zona */
    post: operations['live_view_token_zones__zone_id__live_view_token_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/plants/{plant_id}/admissions': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Evaluaciones de la prueba de admisión de la planta, la más reciente primero */
    get: operations['list_admissions_plants__plant_id__admissions_get'];
    put?: never;
    /** Prueba de admisión de tres preguntas de una familia en la planta */
    post: operations['evaluate_plants__plant_id__admissions_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/documents': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Concesión de subida de un documento firmado de planta (un solo PUT, 15 minutos) */
    post: operations['grant_document_documents_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/gates': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Estado de las compuertas de montaje y de uso de la zona y su modo resultante */
    get: operations['gate_state_zones__zone_id__gates_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/gates/mounting/scope-record': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Acta de alcance: aprueba la compuerta de montaje (la zona pasa a comisionamiento) */
    post: operations['scope_record_zones__zone_id__gates_mounting_scope_record_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/gates/{gate}/revocation': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Revoca la compuerta de montaje o de uso con motivo; lo ya escrito permanece */
    post: operations['revocation_zones__zone_id__gates__gate__revocation_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/plants/{plant_id}/policy': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Política de hallazgos incerrables vigente de la planta (loaded = false si no hay) */
    get: operations['plant_policy_plants__plant_id__policy_get'];
    put?: never;
    /** Carga la versión siguiente de la política de hallazgos incerrables de la planta */
    post: operations['sign_policy_plants__plant_id__policy_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/catalog': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Catálogo vigente de la zona con su motivo, autor y fecha */
    get: operations['current_catalog_zones__zone_id__catalog_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/catalog/versions': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Historial de versiones del catálogo de la zona, la más reciente primero */
    get: operations['catalog_history_zones__zone_id__catalog_versions_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/catalog/versions/{catalog_version}': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Versión del catálogo con su sobre firmado tal como se emitió */
    get: operations['catalog_version_zones__zone_id__catalog_versions__catalog_version__get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/catalog/single-occupancy': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    /** Marca unipersonal y ventana de agregación de la zona (no marca regresión) */
    put: operations['single_occupancy_zones__zone_id__catalog_single_occupancy_put'];
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/standards': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Declara un estándar en la zona: versión nueva del catálogo firmado */
    post: operations['new_standard_zones__zone_id__standards_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/standards/{standard_id}/versions': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Versión nueva de un estándar: la anterior permanece consultable */
    post: operations['new_standard_version_zones__zone_id__standards__standard_id__versions_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/standards/{standard_id}/retirement': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Retira un estándar: versión nueva del catálogo sin él, nunca un borrado */
    post: operations['retirement_zones__zone_id__standards__standard_id__retirement_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/cameras': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    /** Cámaras de la zona: versión nueva del catálogo y regresión pendiente */
    put: operations['cameras_zones__zone_id__cameras_put'];
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/minimum-coverage': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    /** Cobertura mínima de la zona: versión nueva del catálogo y regresión pendiente */
    put: operations['minimum_coverage_zones__zone_id__minimum_coverage_put'];
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/signals': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    /** Señales declaradas de la zona: versión nueva del catálogo y regresión pendiente */
    put: operations['signals_zones__zone_id__signals_put'];
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/thresholds': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    /** Umbrales de revisión y publicación: versión nueva y regresión pendiente */
    put: operations['thresholds_zones__zone_id__thresholds_put'];
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/windows': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    /** Ventana de clip y parámetros de episodio: versión nueva y regresión pendiente */
    put: operations['windows_zones__zone_id__windows_put'];
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/regression': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Estado de la regresión del walk-test de la zona (no bloquea la operación) */
    get: operations['regression_state_zones__zone_id__regression_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/framing-recaptures': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Recaptura del encuadre de una cámara: regresión con la matriz completa */
    post: operations['framing_recapture_zones__zone_id__framing_recaptures_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/plants/{plant_id}/signatory-policy': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Política de firmantes de la planta (configured = false si no hay) */
    get: operations['signatory_policy_plants__plant_id__signatory_policy_get'];
    /** Fija la política de firmantes del acuerdo de uso de la planta */
    put: operations['put_signatory_policy_plants__plant_id__signatory_policy_put'];
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/use-agreements': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Registra el acuerdo de uso de la zona con sus firmantes esperados */
    post: operations['create_agreement_zones__zone_id__use_agreements_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/use-agreements/{agreement_id}/confirmations': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Confirma la firma del usuario de la sesión en el acuerdo de uso */
    post: operations['confirm_agreement_use_agreements__agreement_id__confirmations_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/use-agreements/{agreement_id}/approval': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Aprueba el acuerdo de uso: la compuerta de uso queda aprobada */
    post: operations['approve_agreement_use_agreements__agreement_id__approval_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/transparency': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Vista de transparencia: alcance declarado, estándares, acuerdo y compuertas */
    get: operations['transparency_zones__zone_id__transparency_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/walk-tests': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Abre la sesión de walk-test de la zona con la matriz derivada del catálogo */
    post: operations['open_walk_test_zones__zone_id__walk_tests_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/walk-tests/current': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Sesión de walk-test en curso, incompleta o reabierta de la zona */
    get: operations['current_walk_test_zones__zone_id__walk_tests_current_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/walk-tests/{session_id}/steps': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Abre un paso cronometrado por el servidor */
    post: operations['start_step_walk_tests__session_id__steps_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/walk-tests/{session_id}/steps/{step_id}/close': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Cierra el paso con la hora del servidor y su corrección anexa, si la hay */
    post: operations['close_step_walk_tests__session_id__steps__step_id__close_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/walk-tests/{session_id}/passes': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Registra un pase de una fila de la matriz (solo anexar) */
    post: operations['record_pass_walk_tests__session_id__passes_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/walk-tests/{session_id}/reopen': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Reabre con motivo una sesión incompleta por inactividad */
    post: operations['reopen_walk_test_walk_tests__session_id__reopen_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/walk-tests/{session_id}/occlusion-tests': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Registra la prueba de oclusión de una cámara o declara la pendiente con motivo */
    post: operations['record_occlusion_test_walk_tests__session_id__occlusion_tests_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/walk-tests/regression-rerun': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Abre la reejecución por regresión con solo las filas afectadas */
    post: operations['open_regression_rerun_zones__zone_id__walk_tests_regression_rerun_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/walk-tests/{session_id}/close': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Cierra el acta de comisionamiento con sus siete guardas */
    post: operations['close_walk_test_walk_tests__session_id__close_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/walk-tests/{session_id}/exposure-samples': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Registra la muestra de exposición de un pase mostrado (reloj del navegador) */
    post: operations['record_exposure_sample_walk_tests__session_id__exposure_samples_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/commissioning-records/{record_id}': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Acta de comisionamiento estructurada (sin responsable) */
    get: operations['commissioning_record_commissioning_records__record_id__get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/commissioning-records/{record_id}/document': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Documento legible del acta de comisionamiento (PDF a demanda) */
    get: operations['commissioning_record_document_commissioning_records__record_id__document_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/plants/{plant_id}/nodes': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Declara un nodo de la planta con sus zonas (y, si procede, reemplaza a otro) */
    post: operations['declare_node_plants__plant_id__nodes_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/nodes/{node_id}/zones': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Asigna una zona de su planta a un nodo en operación */
    post: operations['assign_zone_nodes__node_id__zones_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/nodes/{node_id}/zones/{zone_id}/unassignment': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Retira con fecha y motivo la asignación de una zona al nodo */
    post: operations['unassign_zone_nodes__node_id__zones__zone_id__unassignment_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/nodes/{node_id}/enrollment-codes': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Emite el código de alta del nodo (se muestra una sola vez) */
    post: operations['issue_enrollment_code_nodes__node_id__enrollment_codes_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/nodes/{node_id}/enrollment-attempts': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Intentos de alta del nodo, el más reciente primero */
    get: operations['list_enrollment_attempts_nodes__node_id__enrollment_attempts_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/nodes/{node_id}/revocation': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Revoca el nodo: efecto inmediato en las rutas del contrato */
    post: operations['revoke_node_nodes__node_id__revocation_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/nodes/{node_id}/decommission': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Da de baja un nodo revocado; conserva todo lo que envió */
    post: operations['decommission_node_nodes__node_id__decommission_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/zones/{zone_id}/commissioning-clips': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Clips de verificación del difuminado de la zona, el más reciente primero */
    get: operations['commissioning_clips_zones__zone_id__commissioning_clips_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/fleet/nodes': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Inventario de flota con sus avisos, por páginas de hasta 100 nodos */
    get: operations['list_fleet_nodes_fleet_nodes_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/fleet/nodes/{node_id}': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Un nodo del inventario con su historia de latidos (90 días) */
    get: operations['fleet_node_fleet_nodes__node_id__get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/plants/{plant_id}/fleet-thresholds': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Umbrales del panel de flota de la planta (valores por defecto si no hay) */
    get: operations['plant_fleet_thresholds_plants__plant_id__fleet_thresholds_get'];
    /** Fija los umbrales del panel de flota de la planta */
    put: operations['put_plant_fleet_thresholds_plants__plant_id__fleet_thresholds_put'];
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/fleet/target-versions': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Publica la versión objetivo de nodos de una planta (ventana informativa) */
    post: operations['publish_target_version_fleet_target_versions_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/fleet/alarms': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Alarmas de flota por transición, activas y recientes, por páginas de hasta 100 */
    get: operations['list_fleet_alarms_fleet_alarms_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/api/nodes/clip-uploads': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Concesión de subida de un clip. */
    post: operations['post_clip_upload'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/api/nodes/clip-uploads/{clip_id}/confirmation': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Confirmación de un clip de verificación. */
    post: operations['post_clip_upload_confirmation'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/api/nodes/credential-rotations': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Rotación de la credencial del nodo. */
    post: operations['post_credential_rotation'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/api/nodes/detection-reviews': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Presentación de una detección para revisión. */
    post: operations['post_detection_review'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/api/nodes/enrollment': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Alta del nodo. */
    post: operations['post_enrollment'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/api/nodes/findings': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Presentación de un hallazgo. */
    post: operations['post_finding'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/api/nodes/heartbeats': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Latido del nodo. */
    post: operations['post_heartbeat'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/api/nodes/observability-events': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Presentación de un evento de observabilidad. */
    post: operations['post_observability_event'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/api/nodes/update-results': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Resultado de una actualización de software. */
    post: operations['post_update_result'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/api/nodes/zones/{zone_id}/catalog': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Catálogo vigente de una zona. */
    get: operations['get_zone_catalog'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/actions/{action_id}/attachments/upload-grant': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Concesión de subida de un adjunto de cierre: un solo PUT de 15 minutos */
    post: operations['upload_grant_loop_actions__action_id__attachments_upload_grant_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/attachments/{attachment_id}/read-url': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** URL de descarga de un adjunto de cierre, vigente 15 minutos (concesión auditada) */
    post: operations['read_url_loop_attachments__attachment_id__read_url_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/plants/{plant_id}/configuration': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Configuración del lazo de la planta: plazos, umbrales, calendario y zona */
    get: operations['configuration_loop_plants__plant_id__configuration_get'];
    /** Cambia la configuración del lazo de la planta con motivo (versión nueva) */
    put: operations['change_configuration_loop_plants__plant_id__configuration_put'];
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/findings/{finding_state_id}/classifications': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Historial completo de clasificaciones de un hallazgo, en orden */
    get: operations['list_classifications_loop_findings__finding_state_id__classifications_get'];
    put?: never;
    /** Clasifica o reclasifica un hallazgo con motivo validado (registro nuevo) */
    post: operations['classify_loop_findings__finding_state_id__classifications_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/review-queue': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Cola de revisión pendiente, por plazo ascendente, por páginas de hasta 200 */
    get: operations['list_review_queue_loop_review_queue_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/review-queue/{detection_id}/resolution': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Resuelve un elemento de la cola de revisión con motivo validado (registro nuevo) */
    post: operations['resolve_loop_review_queue__detection_id__resolution_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/findings/{finding_state_id}/actions': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Asigna (o reasigna) la acción correctiva de un hallazgo con plazo congelado */
    post: operations['assign_loop_findings__finding_state_id__actions_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/actions/{action_id}/evidence': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Añade una entrada de evidencia (descripción estructurada y adjuntos verificados) */
    post: operations['add_evidence_loop_actions__action_id__evidence_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/findings/{finding_state_id}/closure': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Cierra (firma) un hallazgo con acción asignada y evidencia */
    post: operations['close_loop_findings__finding_state_id__closure_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/findings/{finding_state_id}/reopening': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Reabre un hallazgo cerrado o incerrable con una acción correctiva nueva */
    post: operations['reopen_loop_findings__finding_state_id__reopening_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/findings/{finding_state_id}/root-cause-analyses': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Inicia el análisis de causa raíz guiado con el contexto objetivo de la zona */
    post: operations['start_loop_findings__finding_state_id__root_cause_analyses_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/root-cause-analyses/{analysis_id}/steps/{step_index}': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    /** Escribe un porqué del análisis (el siguiente o uno ya completo) */
    put: operations['put_step_loop_root_cause_analyses__analysis_id__steps__step_index__put'];
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/root-cause-analyses/{analysis_id}/signature': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Firma el análisis con nombre y licencia, o lo deja pendiente de información */
    post: operations['signature_loop_root_cause_analyses__analysis_id__signature_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/root-cause-analyses/{analysis_id}/assistant-requests': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Pide al asistente un borrador de hipótesis para el análisis (asíncrono) */
    post: operations['request_assistant_loop_root_cause_analyses__analysis_id__assistant_requests_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/assistant-requests/{request_id}': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Estado de una solicitud al asistente y, si está lista, su borrador con citas */
    get: operations['assistant_status_loop_assistant_requests__request_id__get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/findings': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Bandeja del lazo por zona, desde la proyección, por páginas de hasta 200 */
    get: operations['list_findings_loop_findings_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/findings/{finding_state_id}': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Detalle auditado de un hallazgo con la cita del estándar y sus degradaciones */
    get: operations['get_finding_loop_findings__finding_state_id__get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/overdue': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Vencidos de la planta por zona, con los de más días destacados */
    get: operations['list_overdue_loop_overdue_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/vocabulary': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Lista de vocabulario bloqueado y vocabulario declarado de la organización */
    get: operations['vocabulary_loop_vocabulary_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/vocabulary/changes': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Cambia la lista de vocabulario con motivo (versión nueva, registrada) */
    post: operations['change_vocabulary_loop_vocabulary_changes_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/findings/{finding_state_id}/unclosable': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Declara SIN ALTERNATIVA SEGURA DISPONIBLE con los cinco campos y la política */
    post: operations['declare_loop_findings__finding_state_id__unclosable_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/loop/unclosable/{declaration_id}/reviews': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Revisa una declaración de incerrable: la renueva o la convierte en acción */
    post: operations['review_loop_unclosable__declaration_id__reviews_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/exports': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Solicita una exportación de una planta (1 a 20 zonas, hasta 366 días) */
    post: operations['request_export_accreditation_exports_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/exports/{export_id}': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Estado y progreso de una exportación, con el resumen del manifiesto si está lista */
    get: operations['export_status_accreditation_exports__export_id__get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/exports/{export_id}/download': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Volúmenes de una exportación lista, con una URL de descarga de 15 min por volumen */
    get: operations['export_download_accreditation_exports__export_id__download_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/exports/{export_id}/download-url': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** URL de descarga de 15 min del documento portátil de una exportación lista */
    post: operations['export_document_url_accreditation_exports__export_id__download_url_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/monthly-packages': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Resumen del paquete mensual sellado de una zona y un mes */
    get: operations['get_monthly_package_accreditation_monthly_packages_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/monthly-packages/{package_id}/sample': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Elementos de la muestra auditada de un paquete mensual sellado */
    get: operations['get_monthly_package_sample_accreditation_monthly_packages__package_id__sample_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/monthly-packages/{plant_id}/{period}/download': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Una URL de descarga de 15 min por zona de la planta con paquete sellado */
    get: operations['get_monthly_package_plant_download_accreditation_monthly_packages__plant_id___period__download_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/sample-items/{sample_item_id}/result': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Registra el resultado de un elemento de la muestra (no toca la clasificación) */
    post: operations['record_sample_result_accreditation_sample_items__sample_item_id__result_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/monthly-packages/{package_id}/commitments': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Registra los compromisos del mando de línea sobre un paquete mensual sellado */
    post: operations['record_commitments_accreditation_monthly_packages__package_id__commitments_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/commitments/{commitment_id}': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    /** Actualiza el estado y la nota de cierre de un compromiso (registro nuevo) */
    patch: operations['update_commitment_accreditation_commitments__commitment_id__patch'];
    trace?: never;
  };
  '/accreditation/commitments': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Compromisos de los paquetes mensuales de una zona y un mes, con su estado */
    get: operations['list_commitments_accreditation_commitments_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/exposure': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Indicador de exposición de una zona y un mes, con su cobertura */
    get: operations['get_exposure_accreditation_exposure_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/adoption': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Actividad del lazo por zona y día, días de silencio y umbral vigente */
    get: operations['get_adoption_accreditation_adoption_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/accreditation/metrics': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Métricas del programa por zona y mes, con su estado */
    get: operations['metrics_accreditation_metrics_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/notifications': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Las notificaciones propias, de la más reciente a la más antigua, por clave */
    get: operations['list_notifications_notifications_get'];
    put?: never;
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/notifications/{notification_id}/read': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    get?: never;
    put?: never;
    /** Marca leído un aviso propio (idempotente) */
    post: operations['mark_notification_read_notifications__notification_id__read_post'];
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
  '/notifications/preferences': {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    /** Las preferencias de correo vigentes del propio actor */
    get: operations['read_preferences_notifications_preferences_get'];
    /** Las preferencias de correo del propio actor (sin campo para silenciar immediate) */
    put: operations['update_preferences_notifications_preferences_put'];
    post?: never;
    delete?: never;
    options?: never;
    head?: never;
    patch?: never;
    trace?: never;
  };
}
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    /**
     * AcceptanceStatus
     * @description Resultado de aceptación de un registro por la plataforma.
     * @enum {string}
     */
    AcceptanceStatus: 'accepted' | 'accepted_duplicate';
    /**
     * AccreditationDetailCode
     * @description Lista cerrada de ``detail_code`` del módulo ``accreditation``.
     * @enum {string}
     */
    AccreditationDetailCode:
      | 'accreditation_export_scope_too_large'
      | 'accreditation_export_zones_span_plants'
      | 'accreditation_export_zone_out_of_scope'
      | 'accreditation_coverage_unavailable'
      | 'accreditation_metric_not_computable'
      | 'accreditation_exposure_not_computable'
      | 'accreditation_sample_already_reviewed'
      | 'accreditation_sample_cannot_modify_classification'
      | 'accreditation_commitment_out_of_scope'
      | 'accreditation_monthly_package_not_available';
    /** ActionOut */
    ActionOut: {
      /**
       * Action Id
       * Format: uuid
       */
      action_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Action Type */
      action_type: string;
      /** Action Description */
      action_description: string;
      /** Expected Outcome */
      expected_outcome: string | null;
      /** State */
      state: string;
      /** Assigned At */
      assigned_at: string;
      /** Due At */
      due_at: string;
      /** Counts As Overdue */
      counts_as_overdue: boolean;
      /** Root Cause Analysis Id */
      root_cause_analysis_id: string | null;
    };
    /** ActivationEnrollment */
    ActivationEnrollment: {
      /** Provisioning Uri */
      provisioning_uri: string;
      /** Qr Svg */
      qr_svg: string;
      /** Recovery Codes */
      recovery_codes: string[];
    };
    /** ActivationResponse */
    ActivationResponse: {
      /**
       * Status
       * @enum {string}
       */
      status: 'pending' | 'activated';
      /** Second Factor Required */
      second_factor_required?: boolean | null;
      enrollment?: components['schemas']['ActivationEnrollment'] | null;
      notice?: components['schemas']['NoticeView'] | null;
    };
    /** ActorOut */
    ActorOut: {
      /** Kind */
      kind: string;
      /**
       * Id
       * Format: uuid
       */
      id: string;
      /** Display Name Snapshot */
      display_name_snapshot: string;
      /** Role In Use */
      role_in_use: string | null;
      /** Concession Id */
      concession_id: string | null;
      /** Unit */
      unit: string;
    };
    /** AdmissionBody */
    AdmissionBody: {
      family: components['schemas']['PredicateFamily'];
      answers: components['schemas']['AnswersBody'];
      /** Justification Es */
      justification_es?: string | null;
    };
    /**
     * AdmissionCriterion
     * @description Criterio de la prueba de admisión que no se cumplió.
     * @enum {string}
     */
    AdmissionCriterion: 'standard' | 'remedy' | 'subject';
    /**
     * AdmissionResult
     * @description Resultado de la prueba de admisión de tres preguntas (H-43).
     * @enum {string}
     */
    AdmissionResult: 'admitted' | 'rejected';
    /**
     * AdmissionView
     * @description Una evaluación de la prueba, admitida o rechazada.
     */
    AdmissionView: {
      /**
       * Admission Id
       * Format: uuid
       */
      admission_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      family: components['schemas']['PredicateFamily'];
      answers: components['schemas']['AnswersBody'];
      /** Justification Es */
      justification_es: string | null;
      result: components['schemas']['AdmissionResult'];
      failed_criterion: components['schemas']['AdmissionCriterion'] | null;
      /**
       * Evaluated By
       * Format: uuid
       */
      evaluated_by: string;
      role_in_use: components['schemas']['Role'];
      /** Evaluated At */
      evaluated_at: string;
      /**
       * Ledger Record Id
       * Format: uuid
       */
      ledger_record_id: string;
    };
    /** AdmissionsPage */
    AdmissionsPage: {
      /** Admissions */
      admissions: components['schemas']['AdmissionView'][];
      /** Next After */
      next_after: string | null;
    };
    /** AdoptionDayOut */
    AdoptionDayOut: {
      /** Day */
      day: string;
      /** Activity Counts */
      activity_counts: {
        [key: string]: number;
      };
      /** Activity Count */
      activity_count: number;
    };
    /** AdoptionReportOut */
    AdoptionReportOut: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Period From */
      period_from: string;
      /** Period To */
      period_to: string;
      /** Evaluated Day */
      evaluated_day: string;
      /** Threshold Days */
      threshold_days: number;
      /** Usage Notice Es */
      usage_notice_es: string;
      /** Zones */
      zones: components['schemas']['AdoptionZoneOut'][];
    };
    /** AdoptionZoneOut */
    AdoptionZoneOut: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Zone Active */
      zone_active: boolean;
      /** Silent Days */
      silent_days: number | null;
      /** Streak Start */
      streak_start: string | null;
      /** Last Activity At */
      last_activity_at: string | null;
      /** Days */
      days: components['schemas']['AdoptionDayOut'][];
    };
    /**
     * AgreementOut
     * @description ``UseAgreement`` (DE §2.8; la forma de ``current_agreement`` de interfaces §1.2).
     */
    AgreementOut: {
      /**
       * Agreement Id
       * Format: uuid
       */
      agreement_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      status: components['schemas']['AgreementStatus'];
      /** Signatories */
      signatories: components['schemas']['SignatoryOut'][];
      /** Document Sha256 */
      document_sha256: string | null;
      /** Replaces Agreement Id */
      replaces_agreement_id: string | null;
      /**
       * Created By
       * Format: uuid
       */
      created_by: string;
      /** Created At */
      created_at: string;
      /** Approved At */
      approved_at: string | null;
      /** Approved By */
      approved_by: string | null;
    };
    /**
     * AgreementStatus
     * @description Ciclo del acuerdo de uso: nunca vuelve atrás.
     * @enum {string}
     */
    AgreementStatus: 'pending_signatures' | 'approved' | 'superseded' | 'revoked';
    /**
     * AlarmStatus
     * @description Filtro de ``GET /fleet/alarms``: abiertas o ya cerradas.
     * @enum {string}
     */
    AlarmStatus: 'active' | 'cleared';
    /**
     * AnalysisSignatureBody
     * @description El texto final que el humano acepta y su licencia profesional.
     */
    AnalysisSignatureBody: {
      /** Professional License */
      professional_license?: string | null;
      /** Root Cause Category */
      root_cause_category?: string | null;
      /** Root Cause Statement */
      root_cause_statement?: string | null;
      /** Conclusion */
      conclusion?: string | null;
      /**
       * Cause Effect Branches
       * @default []
       */
      cause_effect_branches: components['schemas']['BranchBody'][];
    };
    /**
     * AnalysisStepBody
     * @description Un porqué: enunciado, referencias de evidencia y, si lo declara, su categoría.
     */
    AnalysisStepBody: {
      /** Statement */
      statement: string;
      /**
       * Evidence Refs
       * @default []
       */
      evidence_refs: components['schemas']['EvidenceRefBody'][];
      /** Category */
      category?: string | null;
    };
    /**
     * AnswersBody
     * @description Las tres preguntas (BR-GOB-13): estándar escrito, remedio de ingeniería o de proceso, y
     *     dato que pierde sentido al desagregarlo por persona.
     */
    AnswersBody: {
      /** Standard */
      standard: boolean;
      /** Remedy */
      remedy: boolean;
      /** Subject */
      subject: boolean;
    };
    /**
     * ApiErrorBody
     * @description Cuerpo JSON de toda respuesta de error (domain-entities §4.4, pendiente nº 33; VIG-344).
     */
    ApiErrorBody: {
      code: components['schemas']['ApiErrorCode'];
      /**
       * Detail Code
       * @default null
       */
      detail_code: string | null;
      /** Message Es */
      message_es: string;
      /**
       * Correlation Id
       * Format: uuid
       */
      correlation_id: string;
      /**
       * Retry After Seconds
       * @default null
       */
      retry_after_seconds: number | null;
      /** @default null */
      rejection: components['schemas']['ApiErrorRejection'] | null;
    };
    /**
     * ApiErrorCode
     * @description ``api_error_code`` (domain-entities §1 con el pendiente nº 33): lista cerrada de U-02.
     * @enum {string}
     */
    ApiErrorCode:
      | 'invalid_request'
      | 'not_found'
      | 'unauthenticated'
      | 'second_factor_required'
      | 'forbidden'
      | 'throttled'
      | 'rate_limited'
      | 'payload_too_large'
      | 'conflict'
      | 'temporarily_unavailable'
      | 'internal_error'
      | 'privacy_notice_required'
      | 'period_too_long'
      | 'zone_without_node'
      | 'storage_unavailable';
    /**
     * ApiErrorRejection
     * @description El rechazo de un validador de texto libre (VIG-344; ``rejection`` de ``AssistantRequest``,
     *     plataforma-aplicacion §2.19): ``code`` es el motivo cerrado del validador (p. ej.
     *     ``blocked_vocabulary_term``); ``term`` y ``suggestion``, el término que cayó y la alternativa,
     *     si el validador los da. Nunca lleva el texto enviado completo.
     */
    ApiErrorRejection: {
      /** Code */
      code: string;
      /**
       * Term
       * @default null
       */
      term: string | null;
      /**
       * Suggestion
       * @default null
       */
      suggestion: string | null;
    };
    /** ApprovalOut */
    ApprovalOut: {
      agreement: components['schemas']['AgreementOut'];
      gates: components['schemas']['GateStateOut'];
    };
    /** AssignedView */
    AssignedView: {
      /**
       * Assignment Id
       * Format: uuid
       */
      assignment_id: string;
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      role: components['schemas']['Role'];
      scope_level: components['schemas']['ScopeLevel'];
      /**
       * Scope Id
       * Format: uuid
       */
      scope_id: string;
    };
    /** AssignmentBody */
    AssignmentBody: {
      role: components['schemas']['Role'];
      scope_level: components['schemas']['ScopeLevel'];
      /**
       * Scope Id
       * Format: uuid
       */
      scope_id: string;
    };
    /** AssignmentView */
    AssignmentView: {
      role: components['schemas']['Role'];
      scope_level: components['schemas']['ScopeLevel'];
      /**
       * Scope Id
       * Format: uuid
       */
      scope_id: string;
    };
    /** AssistantRequestOut */
    AssistantRequestOut: {
      /**
       * Request Id
       * Format: uuid
       */
      request_id: string;
      /**
       * Analysis Id
       * Format: uuid
       */
      analysis_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Assistant State */
      assistant_state: string;
      /** Unavailable Cause */
      unavailable_cause: string | null;
      /** Requested At */
      requested_at: string;
      /** Resolved At */
      resolved_at: string | null;
      /** Attempt Count */
      attempt_count: number;
      draft: components['schemas']['DraftOut'] | null;
    };
    /** AuditCursorOut */
    AuditCursorOut: {
      /** Occurred At */
      occurred_at: string;
      /**
       * Entry Id
       * Format: uuid
       */
      entry_id: string;
    };
    /** AuditEntryOut */
    AuditEntryOut: {
      /**
       * Entry Id
       * Format: uuid
       */
      entry_id: string;
      /** Chain Sequence */
      chain_sequence: number;
      actor: components['schemas']['ActorOut'];
      /** Operation */
      operation: string;
      scope: components['schemas']['ScopeOut'];
      /** Resource Kind */
      resource_kind: string | null;
      /** Resource Id */
      resource_id: string | null;
      filters: components['schemas']['JsonValue'];
      /** Result Count */
      result_count: number | null;
      /** Outcome */
      outcome: string;
      /**
       * Correlation Id
       * Format: uuid
       */
      correlation_id: string;
      /** Occurred At */
      occurred_at: string;
      /** Previous Hash */
      previous_hash: string;
      /** Entry Hash */
      entry_hash: string;
    };
    /** AuditEntryPage */
    AuditEntryPage: {
      /** Items */
      items: components['schemas']['AuditEntryOut'][];
      next_cursor: components['schemas']['AuditCursorOut'] | null;
    };
    /** BaselineBody */
    BaselineBody: {
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /**
       * Captured At
       * Format: date-time
       */
      captured_at: string;
    };
    /** BaselineOut */
    BaselineOut: {
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Captured At */
      captured_at: string;
    };
    /** BeginActivation */
    BeginActivation: {
      /**
       * @description discriminator enum property added by openapi-typescript
       * @enum {string}
       */
      step: 'begin';
    };
    /**
     * BlurVerificationBody
     * @description Parte declarada del difuminado (D-2). Los dos campos son opcionales en la forma para que
     *     su ausencia responda ``catalog_blur_not_verified``, no ``invalid_request``.
     */
    BlurVerificationBody: {
      /** Declared */
      declared?: boolean | null;
      /** Capture Document Ref */
      capture_document_ref?: {
        [key: string]: unknown;
      } | null;
    };
    /**
     * BranchBody
     * @description Una rama del diagrama de causa y efecto.
     */
    BranchBody: {
      /** Branch Category */
      branch_category: string;
      /** Factors */
      factors: components['schemas']['FactorBody'][];
    };
    /** BranchOut */
    BranchOut: {
      /** Branch Category */
      branch_category: string;
      /** Factors */
      factors: components['schemas']['FactorOut'][];
    };
    /** BusinessCalendarBody */
    BusinessCalendarBody: {
      /** Working Weekdays */
      working_weekdays: number[];
      /** Non Working Dates */
      non_working_dates: string[];
    };
    /** BusinessCalendarOut */
    BusinessCalendarOut: {
      /** Working Weekdays */
      working_weekdays: number[];
      /** Non Working Dates */
      non_working_dates: string[];
    };
    /** CameraBody */
    CameraBody: {
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /** Code */
      code: string;
      role_in_zone: components['schemas']['CameraRoleInZone'];
      /** Declared Min Fps */
      declared_min_fps: number;
      /** Stream Reference */
      stream_reference: string;
    };
    /** CameraFramingBody */
    CameraFramingBody: {
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /** Framing Description Es */
      framing_description_es: string;
      /** Reference Marker */
      reference_marker: boolean;
    };
    /** CameraMeasuredOut */
    CameraMeasuredOut: {
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /** Measured Fps */
      measured_fps: number | null;
      /** Declared Min Fps */
      declared_min_fps: number | null;
    };
    /**
     * CameraRoleInZone
     * @description Rol de una cámara dentro de su zona.
     * @enum {string}
     */
    CameraRoleInZone: 'primary' | 'redundant';
    /** CameraView */
    CameraView: {
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /** Code */
      code: string | null;
      /** Connected */
      connected: boolean;
      /** Measured Fps */
      measured_fps: number;
      /** Declared Min Fps */
      declared_min_fps: number;
      /** Observability State */
      observability_state: string;
    };
    /** CamerasBody */
    CamerasBody: {
      /** Cameras */
      cameras: components['schemas']['CameraBody'][];
      /** Reason Es */
      reason_es: string;
    };
    /**
     * CatalogCamera
     * @description Cámara declarada en el catálogo de la zona.
     */
    CatalogCamera: {
      camera_id: components['schemas']['UUID'];
      code: components['schemas']['Code'];
      role_in_zone: components['schemas']['CameraRoleInZone'];
      /** Declared Min Fps */
      declared_min_fps: number;
    };
    /**
     * CatalogChangedField
     * @description Qué cambió de una versión del catálogo a la siguiente.
     * @enum {string}
     */
    CatalogChangedField:
      | 'standards'
      | 'cameras'
      | 'minimum_coverage'
      | 'signals'
      | 'thresholds'
      | 'clip_window'
      | 'episode'
      | 'single_occupancy';
    /** CatalogHistoryOut */
    CatalogHistoryOut: {
      /** Versions */
      versions: components['schemas']['CatalogVersionSummary'][];
      /** Next After */
      next_after: string | null;
    };
    /**
     * CatalogVersion
     * @description Versión disponible del catálogo de una zona.
     */
    CatalogVersion: {
      zone_id: components['schemas']['UUID'];
      /** Version */
      version: number;
    };
    /** CatalogVersionDetail */
    CatalogVersionDetail: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Catalog Version */
      catalog_version: number;
      /** Issued At */
      issued_at: string;
      /**
       * Issued By
       * Format: uuid
       */
      issued_by: string;
      role_in_use: components['schemas']['Role'];
      /** Reason Es */
      reason_es: string;
      /** Changed Fields */
      changed_fields: components['schemas']['CatalogChangedField'][];
      /** Superseded At */
      superseded_at: string | null;
      /** Single Occupancy */
      single_occupancy: boolean;
      /** Aggregation Window Minutes */
      aggregation_window_minutes: number;
      /** Catalog */
      catalog: {
        [key: string]: unknown;
      };
      /** Envelope */
      envelope: {
        [key: string]: unknown;
      };
    };
    /**
     * CatalogVersionOut
     * @description La versión con su ``ZoneCatalog`` y los atributos de plataforma que no viajan al nodo.
     */
    CatalogVersionOut: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Catalog Version */
      catalog_version: number;
      /** Issued At */
      issued_at: string;
      /**
       * Issued By
       * Format: uuid
       */
      issued_by: string;
      role_in_use: components['schemas']['Role'];
      /** Reason Es */
      reason_es: string;
      /** Changed Fields */
      changed_fields: components['schemas']['CatalogChangedField'][];
      /** Superseded At */
      superseded_at: string | null;
      /** Single Occupancy */
      single_occupancy: boolean;
      /** Aggregation Window Minutes */
      aggregation_window_minutes: number;
      /** Catalog */
      catalog: {
        [key: string]: unknown;
      };
    };
    /**
     * CatalogVersionSummary
     * @description Una entrada del historial: qué cambió, por qué, quién y cuándo (H-44).
     */
    CatalogVersionSummary: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Catalog Version */
      catalog_version: number;
      /** Issued At */
      issued_at: string;
      /**
       * Issued By
       * Format: uuid
       */
      issued_by: string;
      role_in_use: components['schemas']['Role'];
      /** Reason Es */
      reason_es: string;
      /** Changed Fields */
      changed_fields: components['schemas']['CatalogChangedField'][];
      /** Superseded At */
      superseded_at: string | null;
    };
    /** CauseCountOut */
    CauseCountOut: {
      /** Category */
      category: string;
      /** Count */
      count: number;
    };
    /** CertificateChainPem */
    CertificateChainPem: string;
    /** CertificatePem */
    CertificatePem: string;
    /** ChainOut */
    ChainOut: {
      /**
       * Kind
       * @enum {string}
       */
      kind: 'ledger' | 'audit';
      /** Plant Id */
      plant_id: string | null;
    };
    /** ChangedByOut */
    ChangedByOut: {
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      /** Display Name */
      display_name: string;
      /** Role In Use */
      role_in_use: string;
    };
    /** CheckpointKeyOut */
    CheckpointKeyOut: {
      /** Key Id */
      key_id: string;
      /**
       * Algorithm
       * @constant
       */
      algorithm: 'Ed25519';
      /** Public Key */
      public_key: string;
      /** Status */
      status: string;
      /** Valid From */
      valid_from: string;
      /** Valid Until */
      valid_until: string;
    };
    /** CheckpointKeysOut */
    CheckpointKeysOut: {
      /**
       * Purpose
       * @constant
       */
      purpose: 'checkpoint';
      /** Keys */
      keys: components['schemas']['CheckpointKeyOut'][];
    };
    /** CheckpointOut */
    CheckpointOut: {
      chain: components['schemas']['ChainOut'];
      /** Sequence */
      sequence: number;
      /**
       * Entry Id
       * Format: uuid
       */
      entry_id: string;
      /** Entry Hash */
      entry_hash: string;
      /** Covered Sequence */
      covered_sequence: number;
      /** Covered Hash */
      covered_hash: string;
      /** Taken At */
      taken_at: string;
      /** Key Id */
      key_id: string;
      /** Signature */
      signature: string;
    };
    /** CheckpointsOut */
    CheckpointsOut: {
      /**
       * Organization Id
       * Format: uuid
       */
      organization_id: string;
      /** Checkpoints */
      checkpoints: components['schemas']['CheckpointOut'][];
    };
    /** CitationOut */
    CitationOut: {
      /** Kind */
      kind: string;
      /** Id */
      id: string;
      /** Quoted Fact Es */
      quoted_fact_es: string;
      /** Reference Id */
      reference_id: string | null;
    };
    /**
     * ClassificationBody
     * @description La decisión: salida, categoría, motivo y, al reclasificar, el motivo reforzado.
     */
    ClassificationBody: {
      /** Outcome */
      outcome?: string | null;
      /** Reason Category */
      reason_category?: string | null;
      /** Reason Text */
      reason_text?: string | null;
      /**
       * Reinforced Reason
       * @default false
       */
      reinforced_reason: boolean;
    };
    /** ClassificationHistoryOut */
    ClassificationHistoryOut: {
      /**
       * Finding State Id
       * Format: uuid
       */
      finding_state_id: string;
      /** Time Aggregated */
      time_aggregated: boolean;
      /** Aggregation Window Minutes */
      aggregation_window_minutes: number | null;
      /** Classifications */
      classifications: components['schemas']['ClassificationItemOut'][];
      /** Review Resolutions */
      review_resolutions: components['schemas']['ReviewResolutionItemOut'][];
    };
    /** ClassificationItemOut */
    ClassificationItemOut: {
      /**
       * Classification Id
       * Format: uuid
       */
      classification_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Sequence In Subject */
      sequence_in_subject: number;
      /** Outcome */
      outcome: string;
      /** Reason Category */
      reason_category: string;
      /** Reason Text */
      reason_text: string;
      /** Is Reclassification */
      is_reclassification: boolean;
      /** Reinforced Reason */
      reinforced_reason: boolean;
      /** Previous Outcome */
      previous_outcome: string | null;
      /** Effect On Loop */
      effect_on_loop: string;
      /** Classified At */
      classified_at: string;
      classified_by: components['schemas']['ClassifiedByOut'];
    };
    /** ClassificationOut */
    ClassificationOut: {
      /**
       * Finding State Id
       * Format: uuid
       */
      finding_state_id: string;
      /**
       * Classification Id
       * Format: uuid
       */
      classification_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Sequence In Subject */
      sequence_in_subject: number;
      /** Outcome */
      outcome: string;
      /** Reason Category */
      reason_category: string;
      /** Is Reclassification */
      is_reclassification: boolean;
      /** Reinforced Reason */
      reinforced_reason: boolean;
      /** Effect On Loop */
      effect_on_loop: string;
      /** Lifecycle State */
      lifecycle_state: string;
      /** Classification Count */
      classification_count: number;
      /** Closed With Later Different Classification */
      closed_with_later_different_classification: boolean;
      /** Classified At */
      classified_at: string;
      /** Time Aggregated */
      time_aggregated: boolean;
    };
    /**
     * ClassifiedByOut
     * @description El usuario de la plataforma que clasificó (P8), nunca una persona observada.
     */
    ClassifiedByOut: {
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      /** Display Name */
      display_name: string;
      /** Role In Use */
      role_in_use: string;
    };
    /** ClipOut */
    ClipOut: {
      /**
       * Evidence Id
       * Format: uuid
       */
      evidence_id: string;
      /**
       * Clip Id
       * Format: uuid
       */
      clip_id: string;
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /** Segment */
      segment: string;
      /** Media Kind */
      media_kind: string;
      /** Content Type */
      content_type: string;
      /** Size Bytes */
      size_bytes: number;
      /** Sha256 */
      sha256: string;
      /** Duration Ms */
      duration_ms: number | null;
      /** Starts At */
      starts_at: string | null;
      /** Ends At */
      ends_at: string | null;
      /** Read Url */
      read_url: string | null;
    };
    /**
     * ClipUploadGrant
     * @description Concesión de subida de un clip, respuesta de `POST clip-uploads` (domain-entities.md §4 y notas fechadas; entradas A-05 y A-29 de la adenda). La plataforma liga `storage_key` a organización, planta, zona y nodo. El nodo hace `PUT` a `upload_url` con **exactamente** las cabeceras de `required_headers`; el almacén rechaza un `PUT` sin la suma de verificación. `expires_at` es como máximo 15 minutos después de la emisión (verificado por la plataforma). `purpose` devuelve el propósito declarado en la solicitud (pendiente nº 32): la evidencia se confirma al presentar su registro; un clip de verificación se confirma con `POST clip-uploads/{clip_id}/confirmation`, que devuelve `VerificationClipReceipt`.
     */
    ClipUploadGrant: {
      clip_id: components['schemas']['UUIDv7'];
      purpose: components['schemas']['ClipUploadPurpose'];
      /** Upload Url */
      upload_url: string;
      /**
       * Method
       * @constant
       */
      method: 'PUT';
      expires_at: components['schemas']['Timestamp'];
      /** Max Size Bytes */
      max_size_bytes: number;
      required_headers: components['schemas']['RequiredHeaders'];
      storage_key: components['schemas']['StorageKey'];
    };
    /**
     * ClipUploadPurpose
     * @description Propósito de una subida de clip (`ClipUploadRequest.purpose` y `ClipUploadGrant.purpose`; pendiente nº 32).
     * @enum {string}
     */
    ClipUploadPurpose: 'evidence' | 'verification';
    /**
     * ClipWindow
     * @description Segundos de video antes y después del tramo que cubren los clips (BR-CTR-39); 10 y 10 por defecto.
     */
    ClipWindow: {
      /** Pre Seconds */
      pre_seconds: number;
      /** Post Seconds */
      post_seconds: number;
    };
    /** ClipWindowBody */
    ClipWindowBody: {
      /**
       * Pre Seconds
       * @default 10
       */
      pre_seconds: number;
      /**
       * Post Seconds
       * @default 10
       */
      post_seconds: number;
    };
    /** ClockView */
    ClockView: {
      /** Synchronized */
      synchronized: boolean;
      /** Offset Ms */
      offset_ms: number;
    };
    /** CloseBody */
    CloseBody: {
      /** Signatures */
      signatures: components['schemas']['SignatureBody'][];
      false_alarm_acceptance?: components['schemas']['FalseAlarmAcceptanceBody'] | null;
      installer_measurements: components['schemas']['InstallerMeasurementsBody'];
    };
    /** CloseStepBody */
    CloseStepBody: {
      correction?: components['schemas']['CorrectionBody'] | null;
    };
    /**
     * ClosureBody
     * @description La descripción del cierre, su nota de verificación y la licencia del firmante.
     */
    ClosureBody: {
      /** Closure Description */
      closure_description: string;
      /** Verification Note */
      verification_note?: string | null;
      /** Professional License */
      professional_license?: string | null;
    };
    /** ClosureOut */
    ClosureOut: {
      /**
       * Finding State Id
       * Format: uuid
       */
      finding_state_id: string;
      /**
       * Closure Id
       * Format: uuid
       */
      closure_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /**
       * Action Id
       * Format: uuid
       */
      action_id: string;
      /** Evidence Entry Ids */
      evidence_entry_ids: string[];
      /** Lifecycle State */
      lifecycle_state: string;
      /** Signed At */
      signed_at: string;
      /** Time Aggregated */
      time_aggregated: boolean;
    };
    /** Code */
    Code: string;
    /**
     * CoexistencePredicate
     * @description Plantilla `coexistence`: `all_of` es exactamente presencia y energía afirmada, en cualquier orden; `min_duration_ms = 0`.
     */
    CoexistencePredicate: {
      /** All Of */
      all_of: (
        components['schemas']['PresenceCondition'] | components['schemas']['EnergyAsserted']
      )[];
      /** Min Duration Ms */
      min_duration_ms: number;
    };
    /**
     * CommissioningClipOut
     * @description Un ``VerificationClip`` de la zona (forma de la «Versión 1.5»).
     */
    CommissioningClipOut: {
      /**
       * Clip Id
       * Format: uuid
       */
      clip_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /**
       * Node Id
       * Format: uuid
       */
      node_id: string;
      /** Received At */
      received_at: string;
      /** Sha256 */
      sha256: string;
      /** Blur Check Result */
      blur_check_result: {
        [key: string]: unknown;
      } | null;
    };
    /** CommissioningClipsOut */
    CommissioningClipsOut: {
      /** Clips */
      clips: components['schemas']['CommissioningClipOut'][];
      /** Next After */
      next_after: string | null;
    };
    /**
     * CommissioningRecordOut
     * @description ``CommissioningRecord`` (DE §2.15 con sus notas): el acta estructurada, sin responsable.
     */
    CommissioningRecordOut: {
      /**
       * Commissioning Record Id
       * Format: uuid
       */
      commissioning_record_id: string;
      /**
       * Session Id
       * Format: uuid
       */
      session_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Catalog Version */
      catalog_version: number;
      kind: components['schemas']['WalkTestKind'];
      /** Passes Per Cell */
      passes_per_cell: number;
      /** Matrix Results */
      matrix_results: components['schemas']['MatrixResultOut'][];
      /** False Negatives Total */
      false_negatives_total: number;
      /** False Alarm Rate Observed */
      false_alarm_rate_observed: number;
      /** False Alarm Threshold */
      false_alarm_threshold: number;
      false_alarm_acceptance: components['schemas']['FalseAlarmAcceptanceOut'] | null;
      latency: components['schemas']['LatencyOut'];
      /** Cameras Measured */
      cameras_measured: components['schemas']['CameraMeasuredOut'][];
      /** Occlusion Summary */
      occlusion_summary: components['schemas']['OcclusionSummaryOut'][];
      /** Total Duration Ms */
      total_duration_ms: number;
      /** Steps Summary */
      steps_summary: components['schemas']['StepKindDurationOut'][];
      installer_measurements: components['schemas']['InstallerMeasurementsOut'];
      /** Signatures */
      signatures: components['schemas']['SignatureOut'][];
      /** Closed At */
      closed_at: string;
    };
    /** CommitmentIn */
    CommitmentIn: {
      /** Text */
      text: string;
      /**
       * Responsible User Id
       * Format: uuid
       */
      responsible_user_id: string;
      /** Due Date */
      due_date: string;
    };
    /** CommitmentOut */
    CommitmentOut: {
      /**
       * Commitment Id
       * Format: uuid
       */
      commitment_id: string;
      /**
       * Package Id
       * Format: uuid
       */
      package_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Period */
      period: string;
      /** Text */
      text: string;
      /**
       * Responsible User Id
       * Format: uuid
       */
      responsible_user_id: string;
      /** Due Date */
      due_date: string;
      /** State */
      state: string;
      /** Recorded At */
      recorded_at: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Closed Note */
      closed_note: string | null;
      /** Closed At */
      closed_at: string | null;
    };
    /** CommitmentStatusOut */
    CommitmentStatusOut: {
      /** Commitment Id */
      commitment_id: string;
      /** State */
      state: string;
    };
    /** CommitmentUpdateBody */
    CommitmentUpdateBody: {
      /** State */
      state: string;
      /** Note */
      note?: string | null;
    };
    /** CommitmentUpdatedOut */
    CommitmentUpdatedOut: {
      commitment: components['schemas']['CommitmentOut'];
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
    };
    /** CommitmentsBody */
    CommitmentsBody: {
      /** Commitments */
      commitments: components['schemas']['CommitmentIn'][];
    };
    /** CommitmentsRecordedOut */
    CommitmentsRecordedOut: {
      /**
       * Commitment Batch Id
       * Format: uuid
       */
      commitment_batch_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /**
       * Package Id
       * Format: uuid
       */
      package_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Commitments */
      commitments: components['schemas']['CommitmentOut'][];
    };
    /**
     * CommunicationState
     * @enum {string}
     */
    CommunicationState: 'unknown' | 'reachable' | 'mute';
    /**
     * CompatibilityResult
     * @description Resultado de la política de compatibilidad de versiones.
     * @enum {string}
     */
    CompatibilityResult:
      | 'accepted'
      | 'accepted_with_notice'
      | 'rejected_major'
      | 'rejected_retired'
      | 'rejected_newer';
    /** CompleteActivation */
    CompleteActivation: {
      /**
       * @description discriminator enum property added by openapi-typescript
       * @enum {string}
       */
      step: 'complete';
      /** Password */
      password: string;
      /** Notice Version */
      notice_version: string;
      /** Second Factor Code */
      second_factor_code?: string | null;
    };
    /**
     * ConcessionStatus
     * @description ``concession_status`` (domain-entities §1).
     * @enum {string}
     */
    ConcessionStatus: 'active' | 'expired' | 'revoked';
    /**
     * ConcessionView
     * @description Una concesión vista por el cliente (con su motivo).
     */
    ConcessionView: {
      /**
       * Concession Id
       * Format: uuid
       */
      concession_id: string;
      /**
       * Provider Organization Id
       * Format: uuid
       */
      provider_organization_id: string;
      /**
       * Provider User Id
       * Format: uuid
       */
      provider_user_id: string;
      scope_level: components['schemas']['ScopeLevel'];
      /**
       * Scope Id
       * Format: uuid
       */
      scope_id: string;
      /** Reason */
      reason: string | null;
      /** Granted At */
      granted_at: string;
      /** Expires At */
      expires_at: string;
      status: components['schemas']['ConcessionStatus'];
      /** Revoked At */
      revoked_at: string | null;
      revoked_by_side: components['schemas']['RevokedBySide'] | null;
    };
    /** ConcessionsView */
    ConcessionsView: {
      /** Concessions */
      concessions: components['schemas']['ConcessionView'][];
    };
    /**
     * ConfigurationChangeBody
     * @description Solo los parámetros que cambian, y el motivo.
     */
    ConfigurationChangeBody: {
      /** Classification Deadline Business Days */
      classification_deadline_business_days?: number | null;
      /** Review Deadline Business Days */
      review_deadline_business_days?: number | null;
      /** Action Deadline Default Days */
      action_deadline_default_days?: number | null;
      /** Action Deadline Min Days */
      action_deadline_min_days?: number | null;
      /** Action Deadline Max Days */
      action_deadline_max_days?: number | null;
      /** Manager Highlight Overdue Days */
      manager_highlight_overdue_days?: number | null;
      /** Unclosable Review Default Months */
      unclosable_review_default_months?: number | null;
      /** Unclosable Review Max Months */
      unclosable_review_max_months?: number | null;
      /** Monthly Package Generation Day */
      monthly_package_generation_day?: number | null;
      /** Sample Percent */
      sample_percent?: number | null;
      /** Sample Min */
      sample_min?: number | null;
      /** Sample Max */
      sample_max?: number | null;
      /** Adoption Silence Days */
      adoption_silence_days?: number | null;
      /** Exposure Coverage Threshold Percent */
      exposure_coverage_threshold_percent?: number | null;
      /** Email Grouping Hours */
      email_grouping_hours?: number | null;
      /** Export Max Days */
      export_max_days?: number | null;
      /** Export Max Zones */
      export_max_zones?: number | null;
      /** Single Occupancy Default Window Minutes */
      single_occupancy_default_window_minutes?: number | null;
      /** Reminder Interval Hours */
      reminder_interval_hours?: number | null;
      business_calendar?: components['schemas']['BusinessCalendarBody'] | null;
      /** Reason */
      reason?: string | null;
    };
    /** ConfigurationOut */
    ConfigurationOut: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Classification Deadline Business Days */
      classification_deadline_business_days: number;
      /** Review Deadline Business Days */
      review_deadline_business_days: number;
      /** Action Deadline Default Days */
      action_deadline_default_days: number;
      /** Action Deadline Min Days */
      action_deadline_min_days: number;
      /** Action Deadline Max Days */
      action_deadline_max_days: number;
      /** Manager Highlight Overdue Days */
      manager_highlight_overdue_days: number;
      /** Unclosable Review Default Months */
      unclosable_review_default_months: number;
      /** Unclosable Review Max Months */
      unclosable_review_max_months: number;
      /** Monthly Package Generation Day */
      monthly_package_generation_day: number;
      /** Sample Percent */
      sample_percent: number;
      /** Sample Min */
      sample_min: number;
      /** Sample Max */
      sample_max: number;
      /** Adoption Silence Days */
      adoption_silence_days: number;
      /** Exposure Coverage Threshold Percent */
      exposure_coverage_threshold_percent: number;
      /** Email Grouping Hours */
      email_grouping_hours: number;
      /** Export Max Days */
      export_max_days: number;
      /** Export Max Zones */
      export_max_zones: number;
      /** Single Occupancy Default Window Minutes */
      single_occupancy_default_window_minutes: number;
      /** Reminder Interval Hours */
      reminder_interval_hours: number;
      business_calendar: components['schemas']['BusinessCalendarOut'];
      /** Timezone */
      timezone: string;
      /** Version */
      version: number;
      /** Changed At */
      changed_at: string;
      changed_by: components['schemas']['ChangedByOut'];
      /** Change Reason */
      change_reason: string;
    };
    /**
     * ConfirmationOrigin
     * @description Pantalla desde la que un firmante confirma su firma (respuesta 5).
     * @enum {string}
     */
    ConfirmationOrigin: 'management' | 'transparency';
    /** ConfirmationOut */
    ConfirmationOut: {
      /**
       * Agreement Id
       * Format: uuid
       */
      agreement_id: string;
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      role_in_use: components['schemas']['Role'];
      /** Confirmed At */
      confirmed_at: string;
      origin: components['schemas']['ConfirmationOrigin'];
    };
    /**
     * ContentType
     * @description Tipo de contenido del archivo, si la concesión lo fija. Valor técnico (tipo MIME) que nunca se muestra a una persona: no tiene etiqueta en `labels.es.json` y `label_es` lo excluye de forma explícita (A-42).
     * @enum {string}
     */
    ContentType: 'video/mp4' | 'image/jpeg';
    /**
     * ContractNotice
     * @description Aviso de compatibilidad (BR-CTR-21). Con `result = accepted_with_notice`, `retires_at` es obligatorio: la versión del nodo se retira en esa fecha. El texto es `message_es` (renombrado desde `message`, nota fechada de 2026-09-23).
     */
    ContractNotice: {
      result: components['schemas']['CompatibilityResult'];
      retires_at?: components['schemas']['Timestamp'] | null;
      /** Message Es */
      message_es: string;
    };
    /** ContractNoticeView */
    ContractNoticeView: {
      /** Result */
      result: string;
      /** Retires At */
      retires_at?: string | null;
    };
    /** CorrectionBody */
    CorrectionBody: {
      /** Started At */
      started_at?: string | null;
      /** Ended At */
      ended_at?: string | null;
      /** Reason Es */
      reason_es: string;
    };
    /** CorrectionOut */
    CorrectionOut: {
      /** Started At */
      started_at: string | null;
      /** Ended At */
      ended_at: string | null;
      /** Reason Es */
      reason_es: string;
      /**
       * Corrected By
       * Format: uuid
       */
      corrected_by: string;
      /** Corrected At */
      corrected_at: string;
    };
    /**
     * CorrectiveActionBody
     * @description La acción: tipo, descripción, resultado esperado, responsable y plazo.
     */
    CorrectiveActionBody: {
      /** Action Type */
      action_type: string;
      /** Action Description */
      action_description: string;
      /** Expected Outcome */
      expected_outcome?: string | null;
      /**
       * Assignee User Id
       * Format: uuid
       */
      assignee_user_id: string;
      /** Deadline Days */
      deadline_days?: number | null;
      /** Supersedes Action Id */
      supersedes_action_id?: string | null;
    };
    /** CorrectiveActionOut */
    CorrectiveActionOut: {
      /**
       * Finding State Id
       * Format: uuid
       */
      finding_state_id: string;
      /**
       * Action Id
       * Format: uuid
       */
      action_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Action Type */
      action_type: string;
      /**
       * Assignee User Id
       * Format: uuid
       */
      assignee_user_id: string;
      /** Deadline Days */
      deadline_days: number;
      /** Due At */
      due_at: string;
      /** Supersedes Action Id */
      supersedes_action_id: string | null;
      /** Lifecycle State */
      lifecycle_state: string;
      /** Assigned At */
      assigned_at: string;
      /** Time Aggregated */
      time_aggregated: boolean;
    };
    /** CoverageAtEventOut */
    CoverageAtEventOut: {
      /** State */
      state: string;
      /** Causes */
      causes: string[];
      /** Layer */
      layer: string;
    };
    /** CoverageShareOut */
    CoverageShareOut: {
      /** State */
      state: string;
      /** Duration Ms */
      duration_ms: number;
    };
    /** CoverageStatusOut */
    CoverageStatusOut: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Instant */
      instant: string;
      /** State */
      state: string;
      /** Causes */
      causes: string[];
      /** Layer */
      layer: string;
      /** Clock Basis */
      clock_basis: string;
      /** Source Record Ids */
      source_record_ids: string[];
    };
    /** CoverageSummaryOut */
    CoverageSummaryOut: {
      /** Observable Ms */
      observable_ms: number;
      /** Degraded Ms */
      degraded_ms: number;
      /** Not Observable Ms */
      not_observable_ms: number;
      /** No Communication Ms */
      no_communication_ms: number;
      /** Never Reported Ms */
      never_reported_ms: number;
      /** Zone Not Active Ms */
      zone_not_active_ms: number;
    };
    /** CoverageTimelineOut */
    CoverageTimelineOut: {
      /**
       * Organization Id
       * Format: uuid
       */
      organization_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Period */
      period: {
        [key: string]: string;
      };
      /** Intervals */
      intervals: components['schemas']['IntervalOut'][];
      /** Node Intervals */
      node_intervals: components['schemas']['IntervalOut'][];
      summary: components['schemas']['SummaryOut'];
    };
    /** CoverageValues */
    CoverageValues: {
      /** Required Count */
      required_count: number;
      /** Required Camera Ids */
      required_camera_ids: string[];
    };
    /**
     * CredentialPolicy
     * @description Ciclo de vida del certificado de cliente (NFR-CTR-13): vigencia de 365 días, rotación iniciada por el nodo 30 días antes del vencimiento y alerta en el inventario si faltan menos de 15 días sin rotar.
     */
    CredentialPolicy: {
      /**
       * Validity Days
       * @constant
       */
      validity_days: 365;
      /**
       * Rotate Before Days
       * @constant
       */
      rotate_before_days: 30;
      /**
       * Alert Before Days
       * @constant
       */
      alert_before_days: 15;
    };
    /** CurrentWalkTestOut */
    CurrentWalkTestOut: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      session: components['schemas']['WalkTestSessionOut'] | null;
    };
    /** DeactivatedView */
    DeactivatedView: {
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      /**
       * Status
       * @constant
       */
      status: 'deactivated';
    };
    /** DeadLetterView */
    DeadLetterView: {
      /** Code */
      code: string;
      /** Count */
      count: number;
    };
    /**
     * DeclaredBy
     * @description Firmante responsable de un estándar: usuario de la plataforma, no persona observada. `display_name` es uno de los textos libres admitidos por BR-CTR-13; `role` es un identificador técnico (código de rol de la plataforma) para cumplir la metapropiedad de BR-CTR-13.
     */
    DeclaredBy: {
      user_id: components['schemas']['UUID'];
      /** Display Name */
      display_name: string;
      role: components['schemas']['TechnicalId'];
    };
    /**
     * DeclaredNameBody
     * @description Un nombre que se declara: exime al patrón de nombre propio del validador de atribución.
     */
    DeclaredNameBody: {
      /** Kind */
      kind: string;
      /** Value */
      value: string;
    };
    /** DeclaredNameOut */
    DeclaredNameOut: {
      /**
       * Name Id
       * Format: uuid
       */
      name_id: string;
      /** Kind */
      kind: string;
      /** Value */
      value: string;
      /** Added In Version */
      added_in_version: number;
    };
    /** DeclaredNodeView */
    DeclaredNodeView: {
      /**
       * Node Id
       * Format: uuid
       */
      node_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Code */
      code: string;
      /** Status */
      status: string;
      /** Declared At */
      declared_at: string;
      /** Replaces Node Id */
      replaces_node_id: string | null;
      /** Assignments */
      assignments: components['schemas']['NodeAssignmentView'][];
    };
    /** DeclaredScopeOut */
    DeclaredScopeOut: {
      /** Scope Text Es */
      scope_text_es: string | null;
      /** Cameras */
      cameras: {
        [key: string]: unknown;
      }[];
      /** Minimum Coverage */
      minimum_coverage: {
        [key: string]: unknown;
      } | null;
    };
    /**
     * DeclaredStandard
     * @description Estándar de seguridad tal como la planta lo declaró (domain-entities.md §3.2). Viaja dentro de `ZoneCatalog.standards`. Cada cambio de texto, predicado o parámetro produce una versión nueva; una versión emitida nunca se edita. `declared_text` es el único texto libre del contrato escrito por la planta. El predicado sigue la gramática booleana cerrada de §3.2.1 y su forma depende de `family`: el esquema empareja cada familia con su plantilla (`oneOf`), así que un predicado con la forma de otra familia es inválido. La gramática no puede expresar, por construcción, ausencia de personas ni negación de presencia (P2), disyunciones, comparaciones o conteos de personas, velocidades, tiempos de ciclo ni ninguna magnitud de productividad (P7).
     */
    DeclaredStandard: {
      standard_id: components['schemas']['UUID'];
      /** Version */
      version: number;
      family: components['schemas']['PredicateFamily'];
      /** Title Es */
      title_es: string;
      /** Declared Text */
      declared_text: string;
      declared_by: components['schemas']['DeclaredBy'];
      effective_from: components['schemas']['Timestamp'];
      /**
       * Tier Policy
       * @constant
       */
      tier_policy: 'tier_1_when_signal_valid';
      predicate: components['schemas']['Predicate'];
    };
    /** DeepLinkOut */
    DeepLinkOut: {
      /** Kind */
      kind: string;
      /**
       * Id
       * Format: uuid
       */
      id: string;
    };
    /** DetailNoticeOut */
    DetailNoticeOut: {
      code: components['schemas']['FindingDetailNotice'];
      /** Since */
      since: string | null;
      /** Cause */
      cause: string | null;
    };
    /**
     * DocumentContentType
     * @description Tipos admitidos (nota del 2026-09-23 de §3.14: ``image/png`` además de PDF y JPEG).
     * @enum {string}
     */
    DocumentContentType: 'application/pdf' | 'image/jpeg' | 'image/png';
    /** DocumentGrantBody */
    DocumentGrantBody: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      kind: components['schemas']['DocumentKind'];
      content_type: components['schemas']['DocumentContentType'];
      /** Size Bytes */
      size_bytes: number;
      /** Sha256 */
      sha256: string;
    };
    /** DocumentGrantOut */
    DocumentGrantOut: {
      /**
       * Document Id
       * Format: uuid
       */
      document_id: string;
      upload: components['schemas']['DocumentUploadOut'];
      document_ref: components['schemas']['DocumentRefOut'];
    };
    /**
     * DocumentKind
     * @description Documento firmado de planta; ``blur_check_capture`` por la nota de §3.14.
     * @enum {string}
     */
    DocumentKind: 'scope_record' | 'use_agreement' | 'plant_policy' | 'blur_check_capture';
    /** DocumentRefOut */
    DocumentRefOut: {
      /**
       * Document Id
       * Format: uuid
       */
      document_id: string;
      /** Storage Key */
      storage_key: string;
      /** Sha256 */
      sha256: string;
      content_type: components['schemas']['DocumentContentType'];
      /** Size Bytes */
      size_bytes: number;
    };
    /** DocumentUploadOut */
    DocumentUploadOut: {
      /**
       * Method
       * @constant
       */
      method: 'PUT';
      /** Url */
      url: string;
      /** Required Headers */
      required_headers: {
        [key: string]: string;
      };
      /** Expires At */
      expires_at: string;
    };
    /**
     * DraftOut
     * @description El borrador del asistente: una propuesta, nunca una decisión (P8).
     */
    DraftOut: {
      /** Is Draft */
      is_draft: boolean;
      /** Draft Text */
      draft_text: string;
      /** Citations */
      citations: components['schemas']['CitationOut'][];
      /** Insufficient Evidence Declared */
      insufficient_evidence_declared: boolean;
      /** Proposed Cause Category */
      proposed_cause_category: string | null;
      /** Model */
      model: string;
      /** Model Version */
      model_version: string;
      /** Created At */
      created_at: string;
    };
    /**
     * DwellPredicate
     * @description Plantilla `dwell`: `all_of` es exactamente presencia y energía afirmada, en cualquier orden; `min_duration_ms` entre 1 000 y 3 600 000.
     */
    DwellPredicate: {
      /** All Of */
      all_of: (
        components['schemas']['PresenceCondition'] | components['schemas']['EnergyAsserted']
      )[];
      /** Min Duration Ms */
      min_duration_ms: number;
    };
    /**
     * Endpoints
     * @description Direcciones de la plataforma que usa el nodo.
     */
    Endpoints: {
      /** Ingest Base Url */
      ingest_base_url: string;
    };
    /**
     * EnergyAsserted
     * @description Condición `{signal_role: energy, value: asserted}`: máquina energizada.
     */
    EnergyAsserted: {
      /**
       * condition_signal_role
       * @constant
       */
      signal_role: 'energy';
      /**
       * condition_signal_value
       * @constant
       */
      value: 'asserted';
    };
    /**
     * EnergyDeasserted
     * @description Condición `{signal_role: energy, value: deasserted}`: máquina en reposo (arranque desde reposo).
     */
    EnergyDeasserted: {
      /**
       * condition_signal_role
       * @constant
       */
      signal_role: 'energy';
      /**
       * condition_signal_value
       * @constant
       */
      value: 'deasserted';
    };
    /** EnrollRequest */
    EnrollRequest: {
      /** Code */
      code?: string | null;
    };
    /** EnrollResponse */
    EnrollResponse: {
      /**
       * Status
       * @enum {string}
       */
      status: 'enrollment_started' | 'authenticated';
      enrollment?: components['schemas']['EnrollmentView'] | null;
    };
    /**
     * EnrollmentAttemptResult
     * @enum {string}
     */
    EnrollmentAttemptResult:
      | 'accepted'
      | 'enrollment_code_used'
      | 'enrollment_code_expired'
      | 'enrollment_code_invalid'
      | 'rate_limited';
    /** EnrollmentAttemptView */
    EnrollmentAttemptView: {
      /**
       * Attempt Id
       * Format: uuid
       */
      attempt_id: string;
      /** Node Id */
      node_id: string | null;
      result: components['schemas']['EnrollmentAttemptResult'];
      /** Hardware Fingerprint */
      hardware_fingerprint: string;
      /** Software Version */
      software_version: string;
      /** Contract Version */
      contract_version: string;
      /** Attempted At */
      attempted_at: string;
    };
    /** EnrollmentAttemptsPage */
    EnrollmentAttemptsPage: {
      /** Attempts */
      attempts: components['schemas']['EnrollmentAttemptView'][];
      /** Next After */
      next_after: string | null;
    };
    /**
     * EnrollmentCodeView
     * @description El código de alta, mostrado **una sola vez**.
     */
    EnrollmentCodeView: {
      /** Code */
      code: string;
      /**
       * Code Id
       * Format: uuid
       */
      code_id: string;
      /** Expires At */
      expires_at: string;
      /** Disclosed At */
      disclosed_at: string;
      /** Node Ca Root Sha256 */
      node_ca_root_sha256: string[];
    };
    /**
     * EnrollmentView
     * @description Lo que se muestra **una sola vez** al inscribirse; nada de esto se guarda en claro.
     */
    EnrollmentView: {
      /** Provisioning Uri */
      provisioning_uri: string;
      /** Qr Svg */
      qr_svg: string;
      /** Recovery Codes */
      recovery_codes: string[];
    };
    /** EpisodeBody */
    EpisodeBody: {
      /**
       * Grouping Window Ms
       * @default 3000
       */
      grouping_window_ms: number;
      /**
       * Max Segment Ms
       * @default 900000
       */
      max_segment_ms: number;
    };
    /** EpisodeDurationsOut */
    EpisodeDurationsOut: {
      /** Episode Ms */
      episode_ms: number | null;
      /** Condition Ms */
      condition_ms: number | null;
    };
    /** EpisodeOut */
    EpisodeOut: {
      /**
       * Episode Id
       * Format: uuid
       */
      episode_id: string;
      /** Segment Record Ids */
      segment_record_ids: string[];
      /** Segment Count */
      segment_count: number;
      /** Expected Segment Count */
      expected_segment_count: number;
      /** Missing Segment Indexes */
      missing_segment_indexes: number[];
      /** Started At */
      started_at: string;
      /** Ended At */
      ended_at: string;
      /** Total Episode Duration Ms */
      total_episode_duration_ms: number;
      /** Total Condition Duration Ms */
      total_condition_duration_ms: number;
      /** Continues Open */
      continues_open: boolean;
    };
    /**
     * EpisodeParameters
     * @description Agrupación de observaciones en episodios de zona y tope de duración de cada tramo (BR-CTR-33, BR-CTR-34).
     */
    EpisodeParameters: {
      /** Grouping Window Ms */
      grouping_window_ms: number;
      /** Max Segment Ms */
      max_segment_ms: number;
    };
    /**
     * EvidenceAttachmentBody
     * @description Un adjunto ya subido; el tipo, el tamaño, la suma y la declaración se verifican después.
     */
    EvidenceAttachmentBody: {
      /**
       * Attachment Id
       * Format: uuid
       */
      attachment_id: string;
      /** Content Type */
      content_type: string;
      /** Size Bytes */
      size_bytes: number;
      /** Sha256 */
      sha256: string;
      /** Filename */
      filename: string;
      /**
       * No Identifiable Person Declared
       * @default false
       */
      no_identifiable_person_declared: boolean;
    };
    /**
     * EvidenceEntryBody
     * @description La descripción estructurada (obligatoria) y los adjuntos (opcionales).
     */
    EvidenceEntryBody: {
      /** Description */
      description: string;
      /** Performed At */
      performed_at: string;
      /** Performed By Role */
      performed_by_role: string;
      /** Verification Note */
      verification_note?: string | null;
      /** Attachments */
      attachments?: components['schemas']['EvidenceAttachmentBody'][];
    };
    /** EvidenceEntryOut */
    EvidenceEntryOut: {
      /**
       * Finding State Id
       * Format: uuid
       */
      finding_state_id: string;
      /**
       * Action Id
       * Format: uuid
       */
      action_id: string;
      /**
       * Evidence Entry Id
       * Format: uuid
       */
      evidence_entry_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Attachment Ids */
      attachment_ids: string[];
      /** Lifecycle State */
      lifecycle_state: string;
      /** Added At */
      added_at: string;
      /** Performed At */
      performed_at: string;
      /** Time Aggregated */
      time_aggregated: boolean;
    };
    /**
     * EvidenceOut
     * @description Metadatos verificados de una evidencia: nunca su clave de almacén, su URL ni sus bytes.
     */
    EvidenceOut: {
      /**
       * Evidence Id
       * Format: uuid
       */
      evidence_id: string;
      /**
       * Clip Id
       * Format: uuid
       */
      clip_id: string;
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /** Sha256 */
      sha256: string;
      /** Size Bytes */
      size_bytes: number;
      /** Content Type */
      content_type: string;
      /** Media Kind */
      media_kind: string;
      /** Duration Ms */
      duration_ms: number | null;
      /** Segment */
      segment: string;
      /** Starts At */
      starts_at: string | null;
      /** Ends At */
      ends_at: string | null;
      /** Verified At */
      verified_at: string;
    };
    /**
     * EvidenceReadUrlResponse
     * @description ``EvidenceReadGrant`` (domain-entities §3.5): no se persiste.
     */
    EvidenceReadUrlResponse: {
      /**
       * Evidence Id
       * Format: uuid
       */
      evidence_id: string;
      /** Url */
      url: string;
      /** Expires At */
      expires_at: string;
    };
    /**
     * EvidenceRefBody
     * @description Una referencia a un hecho de la zona: nunca su contenido (§2.9).
     */
    EvidenceRefBody: {
      /** Kind */
      kind: string;
      /**
       * Id
       * Format: uuid
       */
      id: string;
    };
    /** EvidenceRefOut */
    EvidenceRefOut: {
      /** Kind */
      kind: string;
      /**
       * Id
       * Format: uuid
       */
      id: string;
    };
    /**
     * EvidenceTier
     * @description Nivel de evidencia de un hallazgo.
     * @enum {string}
     */
    EvidenceTier: 'tier_1' | 'tier_2';
    /**
     * ExportConflictOut
     * @description 409: el cuerpo de error común más el estado que se muestra de la exportación.
     */
    ExportConflictOut: {
      /** Code */
      code: string;
      /** Message Es */
      message_es: string;
      /**
       * Correlation Id
       * Format: uuid
       */
      correlation_id: string;
      export_state: components['schemas']['ExportState'];
    };
    /** ExportDocumentUrlOut */
    ExportDocumentUrlOut: {
      /**
       * Export Id
       * Format: uuid
       */
      export_id: string;
      /** Document Sha256 */
      document_sha256: string;
      /** Size Bytes */
      size_bytes: number;
      /** Url */
      url: string;
      /** Expires At */
      expires_at: string;
    };
    /** ExportDownloadOut */
    ExportDownloadOut: {
      /**
       * Export Id
       * Format: uuid
       */
      export_id: string;
      /** Archive Sha256 */
      archive_sha256: string;
      /** Volumes */
      volumes: components['schemas']['VolumeDownloadView'][];
    };
    /** ExportRequestBody */
    ExportRequestBody: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Zone Ids */
      zone_ids: string[];
      period: components['schemas']['PeriodBody'];
      package_form: components['schemas']['PackageForm'];
    };
    /**
     * ExportState
     * @description ``export_state`` (respuesta 11): manda sobre el de U-05; sin ``composing`` (U04-H-02).
     * @enum {string}
     */
    ExportState: 'requested' | 'in_progress' | 'ready' | 'failed' | 'expired';
    /** ExportStatusOut */
    ExportStatusOut: {
      /**
       * Export Id
       * Format: uuid
       */
      export_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Zone Ids */
      zone_ids: string[];
      period: components['schemas']['PeriodView'];
      package_form: components['schemas']['PackageForm'];
      export_state: components['schemas']['ExportState'];
      /** Progress Percent */
      progress_percent: number;
      /** Current Artifact */
      current_artifact: string | null;
      /** Attempt Count */
      attempt_count: number;
      /** Failure Code */
      failure_code: string | null;
      /** Requested At */
      requested_at: string;
      /** Started At */
      started_at: string | null;
      /** Completed At */
      completed_at: string | null;
      /** Expires At */
      expires_at: string;
      /**
       * Correlation Id
       * Format: uuid
       */
      correlation_id: string;
      manifest_summary: components['schemas']['ManifestSummaryView'] | null;
    };
    /**
     * ExposureCoverageAnnotationOut
     * @description Un tramo de compuerta de uso revocada (o de acta no vigente) sobre la línea de tiempo.
     */
    ExposureCoverageAnnotationOut: {
      /** Kind */
      kind: string;
      /** From */
      from: string;
      /** To */
      to: string;
      /** Cause */
      cause: string | null;
    };
    /** ExposureCoverageIntervalOut */
    ExposureCoverageIntervalOut: {
      /** From */
      from: string;
      /** To */
      to: string;
      /** State */
      state: string;
      /** Causes */
      causes: string[];
      /** Layer */
      layer: string;
      /** Duration Ms */
      duration_ms: number;
    };
    /** ExposureCoverageSummaryOut */
    ExposureCoverageSummaryOut: {
      /** Observable Ms */
      observable_ms: number;
      /** Degraded Ms */
      degraded_ms: number;
      /** Not Observable Ms */
      not_observable_ms: number;
      /** No Communication Ms */
      no_communication_ms: number;
      /** Never Reported Ms */
      never_reported_ms: number;
      /** Zone Not Active Ms */
      zone_not_active_ms: number;
      /** Not Elapsed Ms */
      not_elapsed_ms: number;
      /** Total Ms */
      total_ms: number;
    };
    /** ExposureCoverageTimelineOut */
    ExposureCoverageTimelineOut: {
      period: components['schemas']['ExposureTimelineRangeOut'];
      /** Intervals */
      intervals: components['schemas']['ExposureCoverageIntervalOut'][];
      summary: components['schemas']['ExposureCoverageSummaryOut'];
      /** Annotations */
      annotations: components['schemas']['ExposureCoverageAnnotationOut'][];
    };
    /**
     * ExposureHourlyBucketOut
     * @description Un cubo de la distribución horaria: solo su inicio en el día local y su ancho.
     */
    ExposureHourlyBucketOut: {
      /** Start Minute Of Day */
      start_minute_of_day: number;
      /** Width Minutes */
      width_minutes: number;
      /** Confirmed Minutes */
      confirmed_minutes: number;
    };
    /** ExposureOut */
    ExposureOut: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Period */
      period: string;
      /** Period From */
      period_from: string;
      /** Period To */
      period_to: string;
      /** Status */
      status: string;
      /** Confirmed Minutes */
      confirmed_minutes: number;
      /** Pending Classification Minutes */
      pending_classification_minutes: number;
      /** Observable Coverage Percent */
      observable_coverage_percent: number;
      /** Partial Reason Es */
      partial_reason_es: string | null;
      /** Usage Notice Es */
      usage_notice_es: string;
      /** Hourly Distribution */
      hourly_distribution: components['schemas']['ExposureHourlyBucketOut'][];
      coverage_timeline: components['schemas']['ExposureCoverageTimelineOut'];
      /** Materialized At */
      materialized_at: string;
      /** Recomputed At */
      recomputed_at: string | null;
    };
    /** ExposureSampleBody */
    ExposureSampleBody: {
      /**
       * Pass Id
       * Format: uuid
       */
      pass_id: string;
      /**
       * Fetched At
       * Format: date-time
       */
      fetched_at: string;
      /**
       * Displayed At
       * Format: date-time
       */
      displayed_at: string;
    };
    /** ExposureSampleOut */
    ExposureSampleOut: {
      /**
       * Sample Id
       * Format: uuid
       */
      sample_id: string;
      /**
       * Session Id
       * Format: uuid
       */
      session_id: string;
      /**
       * Pass Id
       * Format: uuid
       */
      pass_id: string;
      /** Fetched At */
      fetched_at: string;
      /** Displayed At */
      displayed_at: string;
      /** Recorded At */
      recorded_at: string;
    };
    /** ExposureTimelineRangeOut */
    ExposureTimelineRangeOut: {
      /** From */
      from: string;
      /** To */
      to: string;
    };
    /** FactorBody */
    FactorBody: {
      /** Text */
      text: string;
      /**
       * Evidence Refs
       * @default []
       */
      evidence_refs: components['schemas']['EvidenceRefBody'][];
    };
    /** FactorOut */
    FactorOut: {
      /** Text */
      text: string;
      /** Evidence Refs */
      evidence_refs: components['schemas']['EvidenceRefOut'][];
    };
    /** FalseAlarmAcceptanceBody */
    FalseAlarmAcceptanceBody: {
      /** Reason Es */
      reason_es: string;
    };
    /** FalseAlarmAcceptanceOut */
    FalseAlarmAcceptanceOut: {
      /** Reason Es */
      reason_es: string;
      /**
       * Accepted By
       * Format: uuid
       */
      accepted_by: string;
      /** Accepted At */
      accepted_at: string;
    };
    /** FamilyCountOut */
    FamilyCountOut: {
      /** Family */
      family: string;
      /** Count */
      count: number;
    };
    /**
     * FindingDetailNotice
     * @description ``finding_detail_notice``: los avisos del detalle de un hallazgo (TASK-342; BR-LAZ-10, 11,
     *     150, 152 y 155; PAT-LAZ-RES-04). Los cuatro primeros son los de la bandeja; los demás declaran
     *     una sección degradada o la versión posterior del estándar. **Ninguno oculta el hallazgo** y
     *     ninguno afirma que la zona estuviera observada.
     * @enum {string}
     */
    FindingDetailNotice:
      | 'agreement_revoked'
      | 'walk_test_regression_pending'
      | 'evidence_flagged'
      | 'missing_segments_declared'
      | 'later_standard_version'
      | 'standard_text_unavailable'
      | 'coverage_unavailable'
      | 'node_state_unavailable'
      | 'evidence_url_unavailable'
      | 'segment_records_limited';
    /** FindingDetailOut */
    FindingDetailOut: {
      /**
       * Finding State Id
       * Format: uuid
       */
      finding_state_id: string;
      /** Subject Kind */
      subject_kind: string;
      /**
       * Subject Id
       * Format: uuid
       */
      subject_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Family */
      family: string;
      /** Tier */
      tier: string;
      lifecycle_state: components['schemas']['FindingLifecycleState'];
      /** Classification Outcome */
      classification_outcome: string | null;
      /** Classification Count */
      classification_count: number;
      /** Age Days */
      age_days: number;
      /** Observability State At Event */
      observability_state_at_event: string;
      /** Occurred At */
      occurred_at: string;
      /** Due At */
      due_at: string | null;
      /** Overdue Days */
      overdue_days: number;
      /** Time Aggregated */
      time_aggregated: boolean;
      /** Aggregation Window Minutes */
      aggregation_window_minutes: number | null;
      /** Evidence Flagged */
      evidence_flagged: boolean;
      standard: components['schemas']['StandardCitationOut'];
      gate_at_event: components['schemas']['GateAtEventOut'];
      coverage_at_event: components['schemas']['CoverageAtEventOut'] | null;
      /** Nodes */
      nodes: components['schemas']['NodeOut'][] | null;
      episode: components['schemas']['EpisodeOut'] | null;
      /** Records */
      records: components['schemas']['RecordOut'][];
      /** Classifications */
      classifications: components['schemas']['ClassificationItemOut'][];
      /** Actions */
      actions: components['schemas']['ActionOut'][];
      /** Analyses */
      analyses: components['schemas']['vigia_platform__loop__adapters__http__findings_routes__AnalysisOut'][];
      /** Notices */
      notices: components['schemas']['DetailNoticeOut'][];
    };
    /**
     * FindingLifecycleState
     * @description ``finding_lifecycle_state``: el estado del hallazgo en el lazo (RF-PLA-02, respuesta 6).
     *
     *     No existe un estado «sin hallazgos» ni «seguro» (P2).
     * @enum {string}
     */
    FindingLifecycleState:
      | 'received'
      | 'in_review'
      | 'discarded'
      | 'open'
      | 'action_assigned'
      | 'overdue'
      | 'closed'
      | 'unclosable'
      | 'suspended_by_reclassification';
    /** FindingNoticeOut */
    FindingNoticeOut: {
      code: components['schemas']['InboxFindingNotice'];
      /** Since */
      since: string | null;
      /** Cause */
      cause: string | null;
    };
    /** FindingsSummaryOut */
    FindingsSummaryOut: {
      /** Received */
      received: number;
      /** Classified */
      classified: number;
      /** Confirmed */
      confirmed: number;
      /** Discarded */
      discarded: number;
      /** Closed */
      closed: number;
      /** Overdue */
      overdue: number;
      /** Unclosable */
      unclosable: number;
      /** Reopened */
      reopened: number;
    };
    /**
     * FleetAlarmKind
     * @description Las ocho clases de alarma de flota (manda sobre las listas de cinco y de siete).
     * @enum {string}
     */
    FleetAlarmKind:
      | 'node_mute'
      | 'queue_over_threshold'
      | 'clock_drift'
      | 'version_retiring'
      | 'simulated_adapter_in_productive'
      | 'certificate_expiring'
      | 'camera_below_min_fps'
      | 'orphan_clips_growing';
    /** FleetAlarmPage */
    FleetAlarmPage: {
      /** Alarms */
      alarms: components['schemas']['FleetAlarmView'][];
      /** Next After */
      next_after: string | null;
    };
    /**
     * FleetAlarmView
     * @description ``FleetAlarm`` (DE §3.9): solo identificadores, la clase y marcas.
     */
    FleetAlarmView: {
      /**
       * Alarm Id
       * Format: uuid
       */
      alarm_id: string;
      alarm_kind: components['schemas']['FleetAlarmKind'];
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /**
       * Node Id
       * Format: uuid
       */
      node_id: string;
      /** Zone Id */
      zone_id: string | null;
      /** Raised At */
      raised_at: string;
      /** Cleared At */
      cleared_at: string | null;
    };
    /** FleetThresholdsBody */
    FleetThresholdsBody: {
      /** Queue Pending Threshold */
      queue_pending_threshold: number;
      /** Queue Age Threshold Minutes */
      queue_age_threshold_minutes: number;
      /** Clock Drift Threshold Ms */
      clock_drift_threshold_ms: number;
    };
    /** FleetThresholdsView */
    FleetThresholdsView: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Queue Pending Threshold */
      queue_pending_threshold: number;
      /** Queue Age Threshold Minutes */
      queue_age_threshold_minutes: number;
      /** Clock Drift Threshold Ms */
      clock_drift_threshold_ms: number;
      /** Configured */
      configured: boolean;
      /** Updated By */
      updated_by: string | null;
      /** Updated At */
      updated_at: string | null;
    };
    /** FramingRecaptureBody */
    FramingRecaptureBody: {
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /**
       * Captured At
       * Format: date-time
       */
      captured_at: string;
      /** Reason Es */
      reason_es: string;
    };
    /** GateAtEventOut */
    GateAtEventOut: {
      /** Status */
      status: string;
      /** Effective From */
      effective_from: string;
      /** Effective Until */
      effective_until: string | null;
    };
    /**
     * GateKind
     * @description Las dos compuertas de una zona.
     * @enum {string}
     */
    GateKind: 'mounting' | 'usage';
    /**
     * GateStateOut
     * @description ``ZoneGateState`` (§2.4): la proyección, sin el sobre (que viaja al nodo por el latido).
     */
    GateStateOut: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      mounting: components['schemas']['MountingGateOut'];
      usage: components['schemas']['UsageGateOut'];
      resulting_mode: components['schemas']['ZoneMode'];
      /** Issued At */
      issued_at: string | null;
    };
    /**
     * GateStatus
     * @description Estado de una compuerta (montaje o uso).
     * @enum {string}
     */
    GateStatus: 'pending' | 'approved' | 'revoked';
    /** GatesOut */
    GatesOut: {
      mounting: components['schemas']['MountingGateOut'];
      usage: components['schemas']['UsageGateOut'];
      resulting_mode: components['schemas']['ZoneMode'];
    };
    /** GrantBody */
    GrantBody: {
      /**
       * Client Organization Id
       * Format: uuid
       */
      client_organization_id: string;
      /**
       * Scope Level
       * @enum {string}
       */
      scope_level: 'organization' | 'plant';
      /**
       * Scope Id
       * Format: uuid
       */
      scope_id: string;
      /** Reason */
      reason: string;
      /** Duration Hours */
      duration_hours?: number | null;
    };
    /**
     * GuardAsserted
     * @description Condición `{signal_role: guard, value: asserted}`: resguardo en su nivel afirmado (declarado cerrado).
     */
    GuardAsserted: {
      /**
       * condition_signal_role
       * @constant
       */
      signal_role: 'guard';
      /**
       * condition_signal_value
       * @constant
       */
      value: 'asserted';
    };
    /**
     * GuardBypassPredicate
     * @description Plantilla `guard_bypass`: `all_of` contiene presencia y resguardo afirmado (declarado cerrado con persona dentro) y, opcionalmente, energía afirmada; en cualquier orden y sin repetir; `min_duration_ms = 0`.
     */
    GuardBypassPredicate: {
      /** All Of */
      all_of: (
        | components['schemas']['PresenceCondition']
        | components['schemas']['GuardAsserted']
        | components['schemas']['EnergyAsserted']
      )[];
      /** Min Duration Ms */
      min_duration_ms: number;
    };
    /**
     * HeartbeatConfiguration
     * @description Parámetros iniciales del latido. `mute_after_seconds` es un valor derivado, `5 × interval_seconds` (300 con el intervalo de 60 s); la plataforma nunca emite una configuración que rompa esa igualdad (entrada A-04 de la adenda, verificada en el modelo). Si `HeartbeatResponse.heartbeat_interval_seconds` cambia el intervalo, el umbral de mudo pasa a cinco veces el intervalo efectivo (BR-CTR-51).
     */
    HeartbeatConfiguration: {
      /** Interval Seconds */
      interval_seconds: number;
      /** Mute After Seconds */
      mute_after_seconds: number;
    };
    /**
     * HeartbeatNotice
     * @description Rótulo del panel para un nodo sin latido reciente (BR-GOB-75): nunca «sin eventos».
     * @enum {string}
     */
    HeartbeatNotice: 'no_heartbeat_since' | 'no_heartbeat_received';
    /**
     * HeartbeatResponse
     * @description Respuesta de `POST heartbeats` (domain-entities.md §2.5 y notas fechadas): lo que el nodo aprende en cada latido. Es el **único canal** por el que la plataforma comunica al nodo cambios de compuerta, de catálogo, de versión objetivo, de claves públicas y de revocación; la plataforma nunca inicia conexiones al nodo (BR-CTR-55). Las compuertas llegan solo por `gate_states`: no tienen ruta propia. `platform_public_keys` es un `SignedEnvelope<PublicKeySet>` firmado con una clave de propósito `key_set` que el nodo ya tiene fijada; un conjunto que no verifica no se aplica. `contract_notice.message_es` es el único texto libre, genérico y en español (BR-CTR-13, BR-CTR-16). La ventana de mantenimiento no viaja en esta respuesta (decisión D-5). Reglas que verifica el modelo: `mute_after_seconds = 5 × heartbeat_interval_seconds` (pendiente nº 35, BR-CTR-51); una entrada de `gate_states` y de `catalog_versions_available` por cada zona del nodo, cada zona a lo sumo una vez.
     */
    HeartbeatResponse: {
      server_time: components['schemas']['Timestamp'];
      /** Gate States */
      gate_states: components['schemas']['SignedGateState'][];
      /** Catalog Versions Available */
      catalog_versions_available: components['schemas']['CatalogVersion'][];
      target_software_version?: components['schemas']['SemVer'] | null;
      contract_notice: components['schemas']['ContractNotice'];
      platform_public_keys: components['schemas']['SignedPublicKeySet'];
      /** Revoked */
      revoked: boolean;
      /** Heartbeat Interval Seconds */
      heartbeat_interval_seconds: number;
      /** Mute After Seconds */
      mute_after_seconds: number;
    };
    /** HeartbeatView */
    HeartbeatView: {
      /**
       * Heartbeat Id
       * Format: uuid
       */
      heartbeat_id: string;
      /** Received At */
      received_at: string;
      /** Sent At */
      sent_at: string;
      /** Payload Summary */
      payload_summary: {
        [key: string]: unknown;
      };
    };
    /** HierarchyOrganizationView */
    HierarchyOrganizationView: {
      /**
       * Organization Id
       * Format: uuid
       */
      organization_id: string;
      /** Code */
      code: string;
      /** Name */
      name: string;
      /** Kind */
      kind: string;
      /** Status */
      status: string;
    };
    /** HierarchyView */
    HierarchyView: {
      organization: components['schemas']['HierarchyOrganizationView'];
      /** Plants */
      plants: components['schemas']['PlantView'][];
      /** Nodes */
      nodes: components['schemas']['NodeView'][];
    };
    /**
     * InboxFindingNotice
     * @description ``inbox_finding_notice``: los avisos de un elemento, desde las columnas enriquecidas de
     *     ``FindingState`` (TASK-307; BR-LAZ-10, 11, 150 y 155).
     * @enum {string}
     */
    InboxFindingNotice:
      | 'agreement_revoked'
      | 'walk_test_regression_pending'
      | 'evidence_flagged'
      | 'missing_segments_declared';
    /** InboxItemOut */
    InboxItemOut: {
      /**
       * Finding State Id
       * Format: uuid
       */
      finding_state_id: string;
      /** Subject Kind */
      subject_kind: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Family */
      family: string;
      /** Tier */
      tier: string;
      standard: components['schemas']['InboxStandardOut'];
      lifecycle_state: components['schemas']['FindingLifecycleState'];
      /** Classification Outcome */
      classification_outcome: string | null;
      /** Classification Count */
      classification_count: number;
      /** Age Days */
      age_days: number;
      /** Observability State At Event */
      observability_state_at_event: string;
      /** Occurred At */
      occurred_at: string;
      /** Due At */
      due_at: string | null;
      /** Overdue Days */
      overdue_days: number;
      /** Time Aggregated */
      time_aggregated: boolean;
      /** Aggregation Window Minutes */
      aggregation_window_minutes: number | null;
      /** Evidence Flagged */
      evidence_flagged: boolean;
      /** Notices */
      notices: components['schemas']['FindingNoticeOut'][];
    };
    /**
     * InboxOrder
     * @enum {string}
     */
    InboxOrder: 'newest_first' | 'oldest_first';
    /** InboxPageOut */
    InboxPageOut: {
      /** Items */
      items: components['schemas']['InboxItemOut'][];
      /** Zones */
      zones: components['schemas']['ZoneOut'][];
      /** Partitions */
      partitions: components['schemas']['PartitionOut'][];
      /** Next Cursor */
      next_cursor: string | null;
    };
    /**
     * InboxPartitionNotice
     * @description ``inbox_partition_notice``: la proyección de una planta que no se puede leer como el
     *     expediente (TASK-307; BR-LAZ-156); la bandeja lo declara en vez de inventar un estado.
     * @enum {string}
     */
    InboxPartitionNotice: 'projection_rebuilding' | 'projection_diverged';
    /** InboxStandardOut */
    InboxStandardOut: {
      /**
       * Standard Id
       * Format: uuid
       */
      standard_id: string;
      /** Version */
      version: number;
    };
    /**
     * InboxZoneNotice
     * @description ``inbox_zone_notice``: lo que la bandeja declara de cada zona (TASK-307; BR-LAZ-04, 149 y
     *     153; P2). **Ninguno afirma que la zona estuviera observada** ni que no ocurriera nada: la
     *     bandeja no lee la cobertura del periodo (cero llamadas, NFR-LAZ-03).
     * @enum {string}
     */
    InboxZoneNotice: 'coverage_unavailable' | 'not_observed' | 'no_node_assigned';
    /** InstallerMeasurementsBody */
    InstallerMeasurementsBody: {
      /** Beacon Latency Ms P95 */
      beacon_latency_ms_p95: number;
      /** Baselines */
      baselines?: components['schemas']['BaselineBody'][];
    };
    /** InstallerMeasurementsOut */
    InstallerMeasurementsOut: {
      /** Beacon Latency Ms P95 */
      beacon_latency_ms_p95: number;
      /**
       * Measured By
       * @default installer
       */
      measured_by: string;
      /** Baselines */
      baselines?: components['schemas']['BaselineOut'][];
    };
    /** IntegrityResultsOut */
    IntegrityResultsOut: {
      /** Results */
      results: components['schemas']['ResultOut'][];
    };
    /**
     * IntervalOut
     * @description ``CoverageInterval`` (domain-entities §3.8).
     */
    IntervalOut: {
      /** Starts At */
      starts_at: string;
      /** Ends At */
      ends_at: string;
      /** Duration Ms */
      duration_ms: number;
      /** Layer */
      layer: string;
      /** State */
      state: string;
      /** Causes */
      causes: string[];
      subject: components['schemas']['SubjectOut'] | null;
      /** Source Record Ids */
      source_record_ids: string[];
      /** Clock Basis */
      clock_basis: string;
      /** Clock Offset Ms */
      clock_offset_ms?: number | null;
    };
    /**
     * InvitationDelivery
     * @enum {string}
     */
    InvitationDelivery: 'email_queued' | 'link_disclosed';
    /** InvitationView */
    InvitationView: {
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      /**
       * Invitation Id
       * Format: uuid
       */
      invitation_id: string;
      /** Expires At */
      expires_at: string;
      delivery: components['schemas']['InvitationDelivery'];
      /** Link */
      link: string | null;
    };
    /** InviteBody */
    InviteBody: {
      /** Email */
      email: string;
      /** Display Name */
      display_name: string;
      /** Professional License */
      professional_license?: string | null;
      /** Assignments */
      assignments: components['schemas']['AssignmentBody'][];
      /**
       * Disclose Link
       * @default false
       */
      disclose_link: boolean;
    };
    JsonValue: unknown;
    /** KeyId */
    KeyId: string;
    /**
     * KeyPurpose
     * @description Propósito de una clave pública de la plataforma (`PublicKey.purpose`).
     * @enum {string}
     */
    KeyPurpose: 'catalog' | 'gate' | 'live_view_token' | 'key_set';
    /** KeyRotatedOut */
    KeyRotatedOut: {
      /** Purpose */
      purpose: string;
      /** Key Id */
      key_id: string;
      /** Public Key */
      public_key: string;
      /** Valid From */
      valid_from: string;
      /** Valid Until */
      valid_until: string;
      /** Previous Key Id */
      previous_key_id: string | null;
      /** Key Set Published */
      key_set_published: boolean;
    };
    /** LabelCursorOut */
    LabelCursorOut: {
      /** Labeled At */
      labeled_at: string;
      /**
       * Label Id
       * Format: uuid
       */
      label_id: string;
    };
    /** LabelOut */
    LabelOut: {
      /**
       * Label Id
       * Format: uuid
       */
      label_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /**
       * Source Record Id
       * Format: uuid
       */
      source_record_id: string;
      /**
       * Subject Record Id
       * Format: uuid
       */
      subject_record_id: string;
      /** Family */
      family: string;
      /** Outcome */
      outcome: string;
      /** Reason Category */
      reason_category: string;
      /** Evidence Ids */
      evidence_ids: string[];
      /** Labeled At */
      labeled_at: string;
      /** Labeled By User Id */
      labeled_by_user_id: string | null;
      /** Labeled By Role */
      labeled_by_role: string | null;
    };
    /** LabelPageOut */
    LabelPageOut: {
      /** Items */
      items: components['schemas']['LabelOut'][];
      next_cursor: components['schemas']['LabelCursorOut'] | null;
    };
    /**
     * LatencyOut
     * @description Los cuatro tramos por separado, cada uno con su reloj; nulo es «no medido».
     */
    LatencyOut: {
      node_tranche?: components['schemas']['TrancheOut'] | null;
      platform_tranche?: components['schemas']['TrancheOut'] | null;
      exposure_tranche?: components['schemas']['TrancheOut'] | null;
      served_tranche?: components['schemas']['TrancheOut'] | null;
      /** Not Measured */
      not_measured?: string[];
      /** Indicative Sum Median Ms */
      indicative_sum_median_ms?: number | null;
      /** Indicative Sum P95 Ms */
      indicative_sum_p95_ms: number;
      /**
       * Indicative
       * @default true
       */
      indicative: boolean;
      /** Repetitions Counted */
      repetitions_counted?: number | null;
    };
    /** LedgerRecordOut */
    LedgerRecordOut: {
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /**
       * Organization Id
       * Format: uuid
       */
      organization_id: string;
      /** Plant Id */
      plant_id: string | null;
      /** Chain Sequence */
      chain_sequence: number;
      /** Record Type */
      record_type: string;
      /** Schema Version */
      schema_version: number;
      actor: components['schemas']['ActorOut'];
      scope: components['schemas']['ScopeOut'];
      /**
       * Correlation Id
       * Format: uuid
       */
      correlation_id: string;
      /** Received At */
      received_at: string;
      /** Occurred At */
      occurred_at: string | null;
      /** Source Key */
      source_key: string | null;
      content: components['schemas']['JsonValue'];
      /** Content Hash */
      content_hash: string;
      /** Previous Hash */
      previous_hash: string;
      /** Record Hash */
      record_hash: string;
      /** Evidences */
      evidences: components['schemas']['EvidenceOut'][];
    };
    /** LedgerRecordPage */
    LedgerRecordPage: {
      /** Items */
      items: components['schemas']['LedgerRecordOut'][];
      next_cursor: components['schemas']['RecordCursorOut'] | null;
    };
    /**
     * LiveStatus
     * @description Respuesta de ``/health/live``.
     */
    LiveStatus: {
      /**
       * Status
       * @constant
       */
      status: 'live';
    };
    /**
     * LiveViewConfiguration
     * @description Parámetros de la vista en vivo del nodo.
     */
    LiveViewConfiguration: {
      /** Token Max Age Seconds */
      token_max_age_seconds: number;
    };
    /** LiveViewTokenOut */
    LiveViewTokenOut: {
      /** Token */
      token: string;
      /** Live View Local Url */
      live_view_local_url: string | null;
      /** Expires At */
      expires_at: string;
    };
    /** LocalQueueView */
    LocalQueueView: {
      /** Pending */
      pending: number;
      /** Oldest Pending At */
      oldest_pending_at?: string | null;
      /** Dead Letter */
      dead_letter: components['schemas']['DeadLetterView'][];
    };
    /** LoginRequest */
    LoginRequest: {
      /** Email */
      email: string;
      /** Password */
      password: string;
    };
    /** LoginResponse */
    LoginResponse: {
      /**
       * Status
       * @enum {string}
       */
      status: 'authenticated' | 'second_factor_required' | 'second_factor_enrollment_required';
    };
    /**
     * LoopActivityKind
     * @description ``loop_activity_kind``: actividad por zona y día, nunca por usuario (respuesta 17).
     * @enum {string}
     */
    LoopActivityKind:
      | 'inbox_opened'
      | 'finding_classified'
      | 'queue_resolved'
      | 'analysis_advanced'
      | 'action_managed'
      | 'finding_closed';
    /**
     * MaintenanceWindowBody
     * @description ``{from, to}``: ``from`` es palabra reservada de Python, de ahí el alias.
     */
    MaintenanceWindowBody: {
      /**
       * From
       * Format: date-time
       */
      from: string;
      /**
       * To
       * Format: date-time
       */
      to: string;
    };
    /** MaintenanceWindowView */
    MaintenanceWindowView: {
      /** From */
      from: string;
      /** To */
      to: string;
    };
    /** ManagedUserView */
    ManagedUserView: {
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      /** Email */
      email: string;
      /** Display Name */
      display_name: string;
      /** Professional License */
      professional_license: string | null;
      /** Status */
      status: string;
      /** Second Factor Required */
      second_factor_required: boolean;
      /** Second Factor Enrolled */
      second_factor_enrolled: boolean;
      /** Assignments */
      assignments: components['schemas']['UserAssignmentView'][];
    };
    /**
     * ManifestSummaryView
     * @description Resumen del manifiesto de un paquete ``ready``.
     */
    ManifestSummaryView: {
      /** Document Sha256 */
      document_sha256: string;
      /** Archive Sha256 */
      archive_sha256: string;
      /** Size Bytes */
      size_bytes: number;
      /** Artifact Count */
      artifact_count: number;
      /** Checkpoint Count */
      checkpoint_count: number;
      /** Verifier Version */
      verifier_version: string;
    };
    /** MatrixResultOut */
    MatrixResultOut: {
      /**
       * Row Id
       * Format: uuid
       */
      row_id: string;
      /** Detected */
      detected: number;
      /** Missed */
      missed: number;
      /** False Alarms */
      false_alarms: number;
      /** Unverifiable Evidence Refs */
      unverifiable_evidence_refs?: string[];
    };
    /**
     * MatrixRowOut
     * @description Una fila de ``matrix_rows`` con el conteo de sus pases.
     */
    MatrixRowOut: {
      /**
       * Row Id
       * Format: uuid
       */
      row_id: string;
      /**
       * Standard Id
       * Format: uuid
       */
      standard_id: string;
      /** Standard Version */
      standard_version: number;
      /** Predicate Conditions */
      predicate_conditions: {
        [key: string]: unknown;
      }[];
      posture: components['schemas']['Posture'];
      /** Required Passes */
      required_passes: number;
      passes: components['schemas']['PassCountsOut'];
    };
    /** MeResponse */
    MeResponse: {
      user: components['schemas']['UserView'];
      organization: components['schemas']['OrganizationView'];
      /** Assignments */
      assignments: components['schemas']['AssignmentView'][];
      /** Effective Permissions */
      effective_permissions: string[];
      /** Concession Id */
      concession_id: string | null;
      /** Idle Expires At */
      idle_expires_at: string;
      /** Absolute Expires At */
      absolute_expires_at: string;
      /** Privacy Notice Version */
      privacy_notice_version: string;
      /** Api Version */
      api_version: string;
      /** Professional License */
      professional_license: string | null;
      /** Second Factor Enrolled */
      second_factor_enrolled: boolean;
    };
    /**
     * MetricKey
     * @description ``metric_key`` (RF-PLA-17, respuesta 14): ninguna mide productividad (P7).
     * @enum {string}
     */
    MetricKey:
      | 'primary_engineering_closures'
      | 'activation'
      | 'adoption'
      | 'classified_in_time'
      | 'overdue'
      | 'recurrence'
      | 'exposure';
    /**
     * MetricStatus
     * @description ``metric_status``: sin insumo completo, ``not_computable``, nunca cero (RF-PLA-19).
     * @enum {string}
     */
    MetricStatus: 'computed' | 'partial' | 'not_computable';
    /**
     * MetricUnit
     * @description ``unit`` de un ``MetricValue`` (§3.4).
     * @enum {string}
     */
    MetricUnit: 'count' | 'ratio' | 'minutes';
    /** MetricValue */
    MetricValue: {
      key: components['schemas']['MetricKey'];
      status: components['schemas']['MetricStatus'];
      unit: components['schemas']['MetricUnit'];
      /** Value */
      value?: number | null;
      /** Numerator */
      numerator?: number | null;
      /** Denominator */
      denominator?: number | null;
      /** Excluded Unclosable Count */
      excluded_unclosable_count: number;
      /** Over 30 Days Count */
      over_30_days_count?: number | null;
      /** Not Computable Reason Es */
      not_computable_reason_es?: string | null;
      detail_code?: components['schemas']['AccreditationDetailCode'] | null;
      /** Partial Reason Es */
      partial_reason_es?: string | null;
    };
    /** MetricsOut */
    MetricsOut: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Period */
      period: string;
      /** Zones */
      zones: components['schemas']['ProgramMetricSnapshot'][];
    };
    /**
     * MinimumCoverage
     * @description Cobertura mínima (RF-CTR-03, RF-BOR-07; entradas A-03 y A-06 de la adenda): la zona la conserva cuando **todas** las cámaras de `required_camera_ids` están observables **y** al menos `required_count` cámaras de la zona están observables en total; una cámara `degraded` o `not_observable` cuenta como no observable. Restricciones que verifica el modelo: `len(required_camera_ids) ≤ required_count ≤ len(cameras)` y toda cámara requerida está en `cameras` (pendiente nº 34).
     */
    MinimumCoverage: {
      /** Required Count */
      required_count: number;
      /** Required Camera Ids */
      required_camera_ids: components['schemas']['UUID'][];
    };
    /** MinimumCoverageBody */
    MinimumCoverageBody: {
      /** Required Count */
      required_count: number;
      /** Required Camera Ids */
      required_camera_ids: string[];
      /** Reason Es */
      reason_es: string;
    };
    /** MonthlyPackageDownloadOut */
    MonthlyPackageDownloadOut: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Period */
      period: string;
      /** Zones */
      zones: components['schemas']['ZoneDownloadOut'][];
    };
    /** MonthlyPackageOut */
    MonthlyPackageOut: {
      /**
       * Package Id
       * Format: uuid
       */
      package_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Period */
      period: string;
      /** State */
      state: string;
      /** Generated At */
      generated_at: string;
      findings_summary: components['schemas']['FindingsSummaryOut'];
      /** Recurrence Summary */
      recurrence_summary: components['schemas']['RecurrenceEntryOut'][];
      /** Coverage Summary */
      coverage_summary: components['schemas']['CoverageShareOut'][];
      /** Commitments From Previous Period */
      commitments_from_previous_period: components['schemas']['CommitmentStatusOut'][];
      /** Audit Sample Id */
      audit_sample_id: string | null;
      /** Record Id */
      record_id: string | null;
    };
    /**
     * MountingGate
     * @description Compuerta de montaje de la zona.
     */
    MountingGate: {
      status: components['schemas']['GateStatus'];
      decided_at?: components['schemas']['Timestamp'] | null;
      record_id?: components['schemas']['UUID'] | null;
    };
    /** MountingGateOut */
    MountingGateOut: {
      status: components['schemas']['GateStatus'];
      /** Decided At */
      decided_at: string | null;
      /** Record Id */
      record_id: string | null;
      /** Decided By */
      decided_by: string | null;
    };
    /** NodeAssignmentView */
    NodeAssignmentView: {
      /**
       * Assignment Id
       * Format: uuid
       */
      assignment_id: string;
      /**
       * Node Id
       * Format: uuid
       */
      node_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Assigned At */
      assigned_at: string;
      /** Unassigned At */
      unassigned_at: string | null;
    };
    /**
     * NodeCamera
     * @description Cámara asignada al nodo. `stream_reference` nombra el flujo; nunca contiene una dirección con credenciales.
     */
    NodeCamera: {
      camera_id: components['schemas']['UUID'];
      code: components['schemas']['Code'];
      stream_reference: components['schemas']['TechnicalId'];
    };
    /** NodeCameraOut */
    NodeCameraOut: {
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /** Code */
      code: string | null;
      /** Connected */
      connected: boolean;
      /** Observability State */
      observability_state: string;
    };
    /**
     * NodeCredential
     * @description Credencial emitida al nodo: respuesta del alta (con `initial_configuration`, `#/$defs/NodeEnrollmentResponse`) y de la rotación (sin `initial_configuration`, `credential_rotation_request.schema.json#/$defs/CredentialRotationResponse`). El sujeto del certificado de cliente contiene `node_id`, `organization_id` y `plant_id` (BR-CTR-47). `server_certificate_pem` lo firma `vigia-node-ca` y se renueva en cada rotación.
     */
    NodeCredential: {
      node_id: components['schemas']['UUID'];
      organization_id: components['schemas']['UUID'];
      plant_id: components['schemas']['UUID'];
      certificate: components['schemas']['CertificatePem'];
      server_certificate_pem: components['schemas']['CertificatePem'];
      ca_chain: components['schemas']['CertificateChainPem'];
      expires_at: components['schemas']['Timestamp'];
      /** Platform Public Keys */
      platform_public_keys: components['schemas']['PublicKey'][];
      initial_configuration?: components['schemas']['NodeInitialConfiguration'] | null;
    };
    /** NodeDeclarationBody */
    NodeDeclarationBody: {
      /** Code */
      code: string;
      /** Zone Ids */
      zone_ids: string[];
      /** Replaces Node Id */
      replaces_node_id?: string | null;
    };
    /** NodeDecommissionView */
    NodeDecommissionView: {
      /**
       * Node Id
       * Format: uuid
       */
      node_id: string;
      /** Revoked At */
      revoked_at: string;
      /** Decommissioned At */
      decommissioned_at: string;
    };
    /**
     * NodeEnrollmentResponse
     * @description Respuesta del alta: `NodeCredential` con `initial_configuration`.
     */
    NodeEnrollmentResponse: {
      node_id: components['schemas']['UUID'];
      organization_id: components['schemas']['UUID'];
      plant_id: components['schemas']['UUID'];
      certificate: components['schemas']['CertificatePem'];
      server_certificate_pem: components['schemas']['CertificatePem'];
      ca_chain: components['schemas']['CertificateChainPem'];
      expires_at: components['schemas']['Timestamp'];
      /** Platform Public Keys */
      platform_public_keys: components['schemas']['PublicKey'][];
      initial_configuration: components['schemas']['NodeInitialConfiguration'];
    };
    /**
     * NodeGroup
     * @description ``group`` de ``POST /fleet/target-versions`` (decisión del redactor, Notes de TASK-226).
     * @enum {string}
     */
    NodeGroup: 'plant';
    /**
     * NodeInitialConfiguration
     * @description Configuración inicial del nodo entregada en el alta. Reglas entre campos que verifica el modelo: `heartbeat.mute_after_seconds = 5 × heartbeat.interval_seconds` (entrada A-04 de la adenda, pendiente nº 35); una entrada de `gate_states` por cada zona de `zones`; `live_view.token_max_age_seconds` nunca menor que la vigencia del token de vista que emite U-02 (600 s).
     */
    NodeInitialConfiguration: {
      /** Zones */
      zones: components['schemas']['SignedZoneCatalog'][];
      /** Gate States */
      gate_states: components['schemas']['SignedGateState'][];
      /** Cameras */
      cameras: components['schemas']['NodeCamera'][];
      endpoints: components['schemas']['Endpoints'];
      heartbeat: components['schemas']['HeartbeatConfiguration'];
      /**
       * Gate Cache Ttl Seconds
       * @constant
       */
      gate_cache_ttl_seconds: 604800;
      /** Sent Records Retention Days */
      sent_records_retention_days: number;
      /** Time Sources */
      time_sources: components['schemas']['TimeSource'][];
      live_view: components['schemas']['LiveViewConfiguration'];
      credential: components['schemas']['CredentialPolicy'];
    };
    /** NodeInventoryDetail */
    NodeInventoryDetail: {
      node: components['schemas']['NodeInventoryView'];
      /** Heartbeat History */
      heartbeat_history: components['schemas']['HeartbeatView'][];
      /** Next History After */
      next_history_after: string | null;
    };
    /** NodeInventoryPage */
    NodeInventoryPage: {
      /** Nodes */
      nodes: components['schemas']['NodeInventoryView'][];
      /** Next After */
      next_after: string | null;
    };
    /**
     * NodeInventoryView
     * @description ``NodeInventory`` (interfaces §3.4 y «Versión 1.5»); sin latido, sus campos son ``null``.
     */
    NodeInventoryView: {
      /**
       * Node Id
       * Format: uuid
       */
      node_id: string;
      /** Code */
      code: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Zones */
      zones: string[];
      /** Status */
      status: string;
      /** Decommissioned At */
      decommissioned_at: string | null;
      /** Replaces Node Id */
      replaces_node_id: string | null;
      /** Software Version */
      software_version: string | null;
      /** Contract Version */
      contract_version: string | null;
      contract_notice: components['schemas']['ContractNoticeView'] | null;
      /** Model Version */
      model_version: string | null;
      /** Last Heartbeat At */
      last_heartbeat_at: string | null;
      communication_state: components['schemas']['CommunicationState'];
      /** Since */
      since: string;
      heartbeat_notice: components['schemas']['HeartbeatNotice'] | null;
      local_queue: components['schemas']['LocalQueueView'] | null;
      clock: components['schemas']['ClockView'] | null;
      signal_reader: components['schemas']['SignalReaderView'] | null;
      /** Cameras */
      cameras: components['schemas']['CameraView'][];
      /** Zone States */
      zone_states: components['schemas']['ZoneStateView'][];
      /** Warnings */
      warnings: components['schemas']['FleetAlarmKind'][];
      /** Target Version */
      target_version: string | null;
      /** Last Update Result */
      last_update_result: string | null;
      /** Live View Local Url */
      live_view_local_url: string | null;
    };
    /** NodeOut */
    NodeOut: {
      /**
       * Node Id
       * Format: uuid
       */
      node_id: string;
      /** Code */
      code: string;
      /** Communication State */
      communication_state: string;
      /** Since */
      since: string;
      /** Last Heartbeat At */
      last_heartbeat_at: string | null;
      /** Cameras */
      cameras: components['schemas']['NodeCameraOut'][];
    };
    /** NodeReasonBody */
    NodeReasonBody: {
      /** Reason Es */
      reason_es: string;
    };
    /** NodeRevocationView */
    NodeRevocationView: {
      /**
       * Node Id
       * Format: uuid
       */
      node_id: string;
      /** Status */
      status: string;
      /** Revoked At */
      revoked_at: string;
    };
    /** NodeStateOut */
    NodeStateOut: {
      /** Communication State */
      communication_state: string;
      /** Last Heartbeat At */
      last_heartbeat_at: string | null;
    };
    /** NodeView */
    NodeView: {
      /**
       * Node Id
       * Format: uuid
       */
      node_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Code */
      code: string;
      /** Status */
      status: string;
      /** Live View Local Url */
      live_view_local_url: string | null;
    };
    /** NodeZoneBody */
    NodeZoneBody: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
    };
    /** NoticeAcceptRequest */
    NoticeAcceptRequest: {
      /** Notice Version */
      notice_version: string;
    };
    /** NoticeAcceptResponse */
    NoticeAcceptResponse: {
      /** Notice Version */
      notice_version: string;
      /** Newly Accepted */
      newly_accepted: boolean;
    };
    /** NoticeView */
    NoticeView: {
      /** Version */
      version: string;
      /** Text */
      text: string;
      /** Pending Legal Text */
      pending_legal_text: boolean;
    };
    /** NotificationOut */
    NotificationOut: {
      /**
       * Notification Id
       * Format: uuid
       */
      notification_id: string;
      /** Event Name */
      event_name: string;
      /** Urgency */
      urgency: string;
      /** Title Es */
      title_es: string;
      /** Body Es */
      body_es: string;
      deep_link_ref: components['schemas']['DeepLinkOut'];
      /** Created At */
      created_at: string;
      /** Read At */
      read_at: string | null;
      /** Archived At */
      archived_at: string | null;
      /** Email Status */
      email_status: string | null;
    };
    /** NotificationPageOut */
    NotificationPageOut: {
      /** Items */
      items: components['schemas']['NotificationOut'][];
      /** Next After */
      next_after: string | null;
    };
    /** NotificationPreferenceOut */
    NotificationPreferenceOut: {
      /** Email Enabled Normal */
      email_enabled_normal: boolean;
      /** Email Enabled Digest */
      email_enabled_digest: boolean;
      /** Weekly Digest Weekday */
      weekly_digest_weekday: number;
      /** Updated At */
      updated_at: string;
      /** Email Suppressed Hard Bounce */
      email_suppressed_hard_bounce: boolean;
      /** Email Suppressed Complaint */
      email_suppressed_complaint: boolean;
    };
    /** NotificationPreferenceUpdateIn */
    NotificationPreferenceUpdateIn: {
      /** Email Enabled Normal */
      email_enabled_normal: boolean;
      /** Email Enabled Digest */
      email_enabled_digest: boolean;
      /** Weekly Digest Weekday */
      weekly_digest_weekday: number;
    };
    /**
     * NotificationStatusFilter
     * @description ``status`` de ``GET /notifications`` (SUP-U05-33).
     * @enum {string}
     */
    NotificationStatusFilter: 'unread' | 'read';
    /** OcclusionSummaryOut */
    OcclusionSummaryOut: {
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      verification: components['schemas']['OcclusionVerification'];
      /** Test Id */
      test_id?: string | null;
    };
    /** OcclusionTestBody */
    OcclusionTestBody: {
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /**
       * Started At
       * Format: date-time
       */
      started_at: string;
      /**
       * Ended At
       * Format: date-time
       */
      ended_at: string;
      /** Declared Reason Es */
      declared_reason_es?: string | null;
    };
    /**
     * OcclusionTestOut
     * @description ``OcclusionTest`` (DE §2.14 y su nota): ``verification`` distingue los cuatro valores.
     */
    OcclusionTestOut: {
      /**
       * Test Id
       * Format: uuid
       */
      test_id: string;
      /**
       * Camera Id
       * Format: uuid
       */
      camera_id: string;
      /** Started At */
      started_at: string;
      /** Ended At */
      ended_at: string;
      /** Deadline */
      deadline: string;
      verification: components['schemas']['OcclusionVerification'];
      /** Correlated Event Ids */
      correlated_event_ids: string[];
      /** Declared Reason Es */
      declared_reason_es: string | null;
      /** Failure Reason */
      failure_reason: string | null;
      /**
       * Recorded By
       * Format: uuid
       */
      recorded_by: string;
    };
    /**
     * OcclusionVerification
     * @description Verificación de la prueba de oclusión; ``pending`` por la nota de §2.14.
     * @enum {string}
     */
    OcclusionVerification: 'verified' | 'declared' | 'failed' | 'pending';
    /** OpenWalkTestBody */
    OpenWalkTestBody: {
      /** Passes Per Cell */
      passes_per_cell: number;
    };
    /** OrganizationView */
    OrganizationView: {
      /**
       * Organization Id
       * Format: uuid
       */
      organization_id: string;
      /** Code */
      code: string;
      /** Name */
      name: string;
      /** Kind */
      kind: string;
    };
    /** OverdueItemOut */
    OverdueItemOut: {
      /**
       * Finding State Id
       * Format: uuid
       */
      finding_state_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Subject Kind */
      subject_kind: string;
      /** Family */
      family: string;
      /** Tier */
      tier: string;
      lifecycle_state: components['schemas']['FindingLifecycleState'];
      /** Declared */
      declared: boolean;
      /**
       * Action Id
       * Format: uuid
       */
      action_id: string;
      /** Action Type */
      action_type: string | null;
      /** Assignee User Id */
      assignee_user_id: string | null;
      /** Assignee Inactive */
      assignee_inactive: boolean;
      /** Due At */
      due_at: string;
      /** Overdue Since */
      overdue_since: string;
      /** Overdue Days */
      overdue_days: number;
      /** Highlighted */
      highlighted: boolean;
    };
    /** OverduePageOut */
    OverduePageOut: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Highlight Threshold Days */
      highlight_threshold_days: number;
      /** Items */
      items: components['schemas']['OverdueItemOut'][];
      /** Zones */
      zones: components['schemas']['OverdueZoneOut'][];
      partition_notice: components['schemas']['InboxPartitionNotice'] | null;
      /** Next Cursor */
      next_cursor: string | null;
    };
    /** OverdueZoneOut */
    OverdueZoneOut: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Overdue Count */
      overdue_count: number;
      /** Highlighted Count */
      highlighted_count: number;
    };
    /**
     * PackageForm
     * @description ``package_form`` de la exportación (nota U04-H-03).
     * @enum {string}
     */
    PackageForm: 'with_clips' | 'without_clips';
    /** PartitionOut */
    PartitionOut: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      notice: components['schemas']['InboxPartitionNotice'];
    };
    /** PassBody */
    PassBody: {
      /**
       * Row Id
       * Format: uuid
       */
      row_id: string;
      result: components['schemas']['PassResult'];
      /** Evidence Ref */
      evidence_ref?: string | null;
    };
    /** PassCountsOut */
    PassCountsOut: {
      /** Detected */
      detected: number;
      /** Missed */
      missed: number;
      /** False Alarm */
      false_alarm: number;
    };
    /** PassOut */
    PassOut: {
      /**
       * Pass Id
       * Format: uuid
       */
      pass_id: string;
      /**
       * Session Id
       * Format: uuid
       */
      session_id: string;
      /**
       * Row Id
       * Format: uuid
       */
      row_id: string;
      result: components['schemas']['PassResult'];
      /** Evidence Ref */
      evidence_ref: string | null;
      /**
       * Recorded By
       * Format: uuid
       */
      recorded_by: string;
      /** Recorded At */
      recorded_at: string;
    };
    /**
     * PassResult
     * @enum {string}
     */
    PassResult: 'detected' | 'missed' | 'false_alarm';
    /** PasswordChangeRequest */
    PasswordChangeRequest: {
      /** Current Password */
      current_password: string;
      /** New Password */
      new_password: string;
    };
    /**
     * PeriodBody
     * @description ``{from, to}``: ``from`` es palabra reservada de Python, de ahí el alias.
     */
    PeriodBody: {
      /**
       * From
       * Format: date-time
       */
      from: string;
      /**
       * To
       * Format: date-time
       */
      to: string;
    };
    /** PeriodOut */
    PeriodOut: {
      /** From */
      from: string;
      /** To */
      to: string;
    };
    /** PeriodView */
    PeriodView: {
      /** From */
      from: string;
      /** To */
      to: string;
    };
    /** PlantBody */
    PlantBody: {
      /** Code */
      code: string;
      /** Name */
      name: string;
      /** Country */
      country: string;
      /** Data Region */
      data_region: string;
      /** Timezone */
      timezone: string;
    };
    /** PlantPolicyBody */
    PlantPolicyBody: {
      /** Version */
      version: number;
      /**
       * Signed At
       * Format: date-time
       */
      signed_at: string;
      /** Signed By Display Name */
      signed_by_display_name: string;
      /** Legal Opinion Reference */
      legal_opinion_reference: string;
      /** Criteria Summary Es */
      criteria_summary_es: string;
      /** Document Ref */
      document_ref: {
        [key: string]: unknown;
      };
    };
    /**
     * PlantPolicyOut
     * @description ``PlantPolicy`` de ``GateQueryPort.plant_policy`` (interfaces §1.2).
     */
    PlantPolicyOut: {
      /** Loaded */
      loaded: boolean;
      /** Policy Id */
      policy_id?: string | null;
      /** Version */
      version?: number | null;
      /** Signed At */
      signed_at?: string | null;
      /** Signed By Display Name */
      signed_by_display_name?: string | null;
      /** Legal Opinion Reference */
      legal_opinion_reference?: string | null;
      /** Document Sha256 */
      document_sha256?: string | null;
      /** Criteria Summary Es */
      criteria_summary_es?: string | null;
    };
    /** PlantView */
    PlantView: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Code */
      code: string;
      /** Name */
      name: string;
      /** Country */
      country: string;
      /** Data Region */
      data_region: string;
      /** Timezone */
      timezone: string;
      /** Status */
      status: string;
      /** Zones */
      zones: components['schemas']['ZoneView'][];
    };
    /**
     * Posture
     * @description Postura de una fila de la matriz del walk-test (respuesta 9).
     * @enum {string}
     */
    Posture: 'standing' | 'crouched' | 'partially_occluded' | 'slow_movement';
    /** Predicate */
    Predicate:
      | components['schemas']['CoexistencePredicate']
      | components['schemas']['GuardBypassPredicate']
      | components['schemas']['DwellPredicate']
      | components['schemas']['StartupTransitionPredicate'];
    /**
     * PredicateFamily
     * @description Familia del predicado de un estándar declarado.
     * @enum {string}
     */
    PredicateFamily: 'coexistence' | 'guard_bypass' | 'dwell' | 'startup_transition';
    /**
     * PresenceCondition
     * @description Condición de presencia en la zona. Solo admite `true`: la gramática no puede afirmar ausencia (P2).
     */
    PresenceCondition: {
      /**
       * Presence
       * @constant
       */
      presence: true;
    };
    /** PreviousActionOut */
    PreviousActionOut: {
      /** Action Type */
      action_type: string;
      /** Outcome */
      outcome: string;
      /** Closed At */
      closed_at?: string | null;
      /** Recurrence After Days */
      recurrence_after_days?: number | null;
    };
    /** ProfileBody */
    ProfileBody: {
      /** Display Name */
      display_name?: string | null;
      /** Professional License */
      professional_license?: string | null;
      /**
       * Clear Professional License
       * @default false
       */
      clear_professional_license: boolean;
    };
    /** ProgramMetricSnapshot */
    ProgramMetricSnapshot: {
      /**
       * Snapshot Id
       * Format: uuid
       */
      snapshot_id: string;
      /**
       * Organization Id
       * Format: uuid
       */
      organization_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Period */
      period: string;
      /** Period From */
      period_from: string | null;
      /** Period To */
      period_to: string | null;
      /**
       * Granularity
       * @default month
       * @constant
       */
      granularity: 'month';
      /** Values */
      values: components['schemas']['MetricValue'][];
      /** Materialized At */
      materialized_at: string | null;
      /** Recomputed At */
      recomputed_at: string | null;
      /** Inputs Digest */
      inputs_digest: string;
    };
    /**
     * ProviderConcessionView
     * @description Una concesión vista por quien se la concedió (sin el motivo).
     */
    ProviderConcessionView: {
      /**
       * Concession Id
       * Format: uuid
       */
      concession_id: string;
      /**
       * Client Organization Id
       * Format: uuid
       */
      client_organization_id: string;
      scope_level: components['schemas']['ScopeLevel'];
      /**
       * Scope Id
       * Format: uuid
       */
      scope_id: string;
      /** Granted At */
      granted_at: string;
      /** Expires At */
      expires_at: string;
      status: components['schemas']['ConcessionStatus'];
      /** Revoked At */
      revoked_at: string | null;
      revoked_by_side: components['schemas']['RevokedBySide'] | null;
    };
    /** ProviderConcessionsView */
    ProviderConcessionsView: {
      /** Concessions */
      concessions: components['schemas']['ProviderConcessionView'][];
    };
    /** ProviderQueriesPage */
    ProviderQueriesPage: {
      /** Queries */
      queries: components['schemas']['ProviderQueryItem'][];
      /** Next After */
      next_after: string | null;
    };
    /** ProviderQueryItem */
    ProviderQueryItem: {
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Received At */
      received_at: string;
      /** Plant Id */
      plant_id: string | null;
      /**
       * Provider User Id
       * Format: uuid
       */
      provider_user_id: string;
      /** Operation */
      operation: string;
      /** Method */
      method: string;
      /** Resource */
      resource: string;
      /** Occurred At */
      occurred_at: string;
      /** Reason */
      reason: string | null;
    };
    /**
     * PublicKey
     * @description Clave pública Ed25519 de la plataforma para verificar catálogos, compuertas, tokens de vista en vivo y conjuntos de claves (domain-entities.md §3.4 y nota de §3.5). Ninguna estructura del contrato lleva una clave privada (BR-CTR-46).
     */
    PublicKey: {
      key_id: components['schemas']['KeyId'];
      /**
       * Algorithm
       * @constant
       */
      algorithm: 'Ed25519';
      /** Public Key */
      public_key: string;
      purpose: components['schemas']['KeyPurpose'];
      valid_from: components['schemas']['Timestamp'];
      valid_until?: components['schemas']['Timestamp'] | null;
    };
    /** ReactivateBody */
    ReactivateBody: {
      /** Assignments */
      assignments: components['schemas']['AssignmentBody'][];
      /**
       * Disclose Link
       * @default false
       */
      disclose_link: boolean;
    };
    /** ReadUrlResponse */
    ReadUrlResponse: {
      /**
       * Attachment Id
       * Format: uuid
       */
      attachment_id: string;
      /** Url */
      url: string;
      /** Expires At */
      expires_at: string;
      /**
       * Content Disposition
       * @default attachment
       * @constant
       */
      content_disposition: 'attachment';
    };
    /**
     * Receipt
     * @description Recibo que la plataforma añade a un registro al aceptarlo (domain-entities.md §2.7); también es el cuerpo de la respuesta de aceptación. El nodo nunca lo rellena. `received_at` ordena la cadena de hashes del expediente.
     */
    Receipt: {
      platform_record_id: components['schemas']['UUID'];
      received_at: components['schemas']['Timestamp'];
      status: components['schemas']['AcceptanceStatus'];
    };
    /** RecordCursorOut */
    RecordCursorOut: {
      /** Received At */
      received_at: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
    };
    /** RecordOut */
    RecordOut: {
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Record Type */
      record_type: string;
      /** Chain Sequence */
      chain_sequence: number;
      /** Content Hash */
      content_hash: string;
      /** Segment Index */
      segment_index: number | null;
      /** Received At */
      received_at: string;
      /** Occurred At */
      occurred_at: string | null;
      /** Signals */
      signals: components['schemas']['vigia_platform__loop__adapters__http__findings_routes__SignalOut'][];
      /** Clips */
      clips: components['schemas']['ClipOut'][];
    };
    /** RecurrenceEntryOut */
    RecurrenceEntryOut: {
      /** Family */
      family: string;
      /** Episodes After Closure */
      episodes_after_closure: number;
      /** Reference Closure Ids */
      reference_closure_ids: string[];
    };
    /**
     * RegressionCause
     * @description Causa de la regresión del walk-test; ``framing_recaptured`` por la nota de §2.16.
     * @enum {string}
     */
    RegressionCause: 'catalog_change' | 'model_version_change' | 'framing_recaptured';
    /**
     * RegressionOut
     * @description ``regression_state`` de la zona (``CatalogQueryPort.regression_state``).
     */
    RegressionOut: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      state: components['schemas']['RegressionState'];
      /** Marked At */
      marked_at: string | null;
      cause: components['schemas']['RegressionCause'] | null;
      /** Catalog Version */
      catalog_version: number | null;
      /** Model Version */
      model_version: string | null;
      /** Affected Row Ids */
      affected_row_ids: string[] | 'all' | null;
    };
    /**
     * RegressionState
     * @enum {string}
     */
    RegressionState: 'current' | 'pending';
    /**
     * RejectionCode
     * @description Código de rechazo de la plataforma. Transitorios (`retryable = true`, BR-CTR-30): `temporarily_unavailable`, `rate_limited` y `storage_unavailable`; todos los demás son permanentes (`retryable = false`).
     * @enum {string}
     */
    RejectionCode:
      | 'schema_invalid'
      | 'payload_too_large'
      | 'contract_version_unsupported'
      | 'contract_version_retired'
      | 'node_not_enrolled'
      | 'node_revoked'
      | 'node_zone_mismatch'
      | 'zone_gate_not_approved'
      | 'clip_missing'
      | 'clip_hash_mismatch'
      | 'clip_too_large'
      | 'clip_not_anonymized'
      | 'enrollment_code_invalid'
      | 'enrollment_code_expired'
      | 'enrollment_code_used'
      | 'signature_invalid'
      | 'idempotency_conflict'
      | 'timestamp_out_of_window'
      | 'temporarily_unavailable'
      | 'rate_limited'
      | 'storage_unavailable';
    /**
     * RejectionCompatibilityResult
     * @description Resultado de la política de compatibilidad que produjo un rechazo por versión (`RejectionResponse.compatibility_result`): subconjunto de `compatibility_result` sin los resultados aceptados (A-42).
     * @enum {string}
     */
    RejectionCompatibilityResult: 'rejected_major' | 'rejected_retired' | 'rejected_newer';
    /**
     * RejectionResponse
     * @description Cuerpo de todo rechazo de la ingesta (domain-entities.md §4 y nota fechada; BR-CTR-30). `retryable` lo fija la clase del código y el esquema lo exige: transitorio (`temporarily_unavailable`, `rate_limited`, `storage_unavailable`; 429 o 503) con `retryable = true`, permanente (todos los demás; 400, 401, 403, 409, 413, 422) con `retryable = false`. `compatibility_result` es opcional y solo aparece en los rechazos por versión, con el resultado que los produjo: `rejected_major` o `rejected_newer` con `code = contract_version_unsupported`, `rejected_retired` con `code = contract_version_retired` (BR-CTR-20, BR-CTR-21). `rejected_newer` nunca es un `code`. `message_es` es genérico: nombra el campo y la regla, nunca contenido del registro, rutas internas ni trazas (BR-CTR-16).
     */
    RejectionResponse: {
      code: components['schemas']['RejectionCode'];
      /** Retryable */
      retryable: boolean;
      /** Field */
      field?: string | null;
      /** Message Es */
      message_es: string;
      /** Retry After Seconds */
      retry_after_seconds?: number | null;
      compatibility_result?: components['schemas']['RejectionCompatibilityResult'] | null;
    };
    /** ReopenBody */
    ReopenBody: {
      /** Reason Es */
      reason_es: string;
    };
    /**
     * ReopeningBody
     * @description El motivo de la reapertura y la acción correctiva nueva (la de SUP-U05-07).
     *
     *     ``reason_text`` es opcional a nivel de esquema (como ``reason_text`` de
     *     ``ClassificationBody``): ausente o en blanco no es ``invalid_request``, es
     *     ``reopening_reason_required`` (BR-LAZ-146bis), y solo el servicio lo distingue.
     */
    ReopeningBody: {
      /** Reason Text */
      reason_text?: string | null;
      /** Action Type */
      action_type: string;
      /** Action Description */
      action_description: string;
      /** Expected Outcome */
      expected_outcome?: string | null;
      /**
       * Assignee User Id
       * Format: uuid
       */
      assignee_user_id: string;
      /** Deadline Days */
      deadline_days?: number | null;
    };
    /** ReopeningOut */
    ReopeningOut: {
      /**
       * Finding State Id
       * Format: uuid
       */
      finding_state_id: string;
      /**
       * Reopening Id
       * Format: uuid
       */
      reopening_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** From State */
      from_state: string;
      /** Previous Closure Id */
      previous_closure_id: string | null;
      /** Previous Declaration Id */
      previous_declaration_id: string | null;
      /** Supersedes Action Id */
      supersedes_action_id: string | null;
      /** Reopened At */
      reopened_at: string;
      /** Time Aggregated */
      time_aggregated: boolean;
      /**
       * Action Id
       * Format: uuid
       */
      action_id: string;
      /** Due At */
      due_at: string;
      /** Lifecycle State */
      lifecycle_state: string;
    };
    /** ReplayOut */
    ReplayOut: {
      /**
       * Event Id
       * Format: uuid
       */
      event_id: string;
      /** Consumer Name */
      consumer_name: string;
      /**
       * Organization Id
       * Format: uuid
       */
      organization_id: string;
      /**
       * Status
       * @constant
       */
      status: 'pending';
    };
    /**
     * RequiredHeaders
     * @description Mapa de cabeceras del `PUT`, a lo sumo 8 entradas. Siempre incluye `x-amz-checksum-sha256` (el SHA-256 declarado en la solicitud, en base64) y `x-amz-meta-vigia-anonymized` con valor `1`, la marca de anonimización del objeto almacenado (BR-CTR-14); la plataforma responde `clip_not_anonymized` si falta. Solo admite las cabeceras declaradas aquí; una nueva entra como propiedad opcional (BR-CTR-23).
     */
    RequiredHeaders: {
      /** X-Amz-Checksum-Sha256 */
      'x-amz-checksum-sha256': string;
      /**
       * X-Amz-Meta-Vigia-Anonymized
       * @constant
       */
      'x-amz-meta-vigia-anonymized': '1';
      'content-type'?: components['schemas']['ContentType'] | null;
    };
    /** RerunBody */
    RerunBody: {
      /** Passes Per Cell */
      passes_per_cell: number;
    };
    /** ResultOut */
    ResultOut: {
      chain: components['schemas']['ChainOut'];
      /** Mode */
      mode: string;
      /** Result */
      result: string;
      /** From Sequence */
      from_sequence: number;
      /** To Sequence */
      to_sequence: number;
      /** Head Sequence */
      head_sequence: number;
      /** Broken Sequence */
      broken_sequence: number | null;
      /** Broken Entry Id */
      broken_entry_id: string | null;
      /** Reason */
      reason: string | null;
      /** Verified At */
      verified_at: string | null;
    };
    /** RetirementBody */
    RetirementBody: {
      /** Reason Es */
      reason_es: string;
      /**
       * Effective From
       * Format: date-time
       */
      effective_from: string;
    };
    /**
     * ReviewActionBody
     * @description La acción nueva de la conversión (T12): el cuerpo de una asignación de TASK-311.
     */
    ReviewActionBody: {
      /** Action Type */
      action_type?: string | null;
      /** Action Description */
      action_description?: string | null;
      /** Expected Outcome */
      expected_outcome?: string | null;
      /** Assignee User Id */
      assignee_user_id?: string | null;
      /** Deadline Days */
      deadline_days?: number | null;
    };
    /** ReviewQueueItemOut */
    ReviewQueueItemOut: {
      /**
       * Detection Id
       * Format: uuid
       */
      detection_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Family */
      family: string;
      /**
       * Standard Id
       * Format: uuid
       */
      standard_id: string;
      /** Standard Version */
      standard_version: number;
      /** Max Confidence */
      max_confidence: number;
      /** Review Threshold */
      review_threshold: number;
      /** Publication Threshold */
      publication_threshold: number;
      /** Review Reason */
      review_reason: string;
      /** Observability State At Event */
      observability_state_at_event: string;
      /** Occurred At */
      occurred_at: string;
      /** Received At */
      received_at: string;
      /** Review Due At */
      review_due_at: string;
      /** Reminders Sent */
      reminders_sent: number;
    };
    /** ReviewQueuePageOut */
    ReviewQueuePageOut: {
      /** Items */
      items: components['schemas']['ReviewQueueItemOut'][];
      /** Next Cursor */
      next_cursor: string | null;
    };
    /**
     * ReviewResolutionBody
     * @description La decisión: salida, categoría y motivo (20 a 1000 caracteres).
     */
    ReviewResolutionBody: {
      /** Outcome */
      outcome?: string | null;
      /** Reason Category */
      reason_category?: string | null;
      /** Reason Text */
      reason_text?: string | null;
    };
    /**
     * ReviewResolutionItemOut
     * @description La resolución de la detección en la cola (T05 o T06): no es una clasificación.
     */
    ReviewResolutionItemOut: {
      /**
       * Resolution Id
       * Format: uuid
       */
      resolution_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Outcome */
      outcome: string;
      /** Reason Category */
      reason_category: string;
      /** Reason Text */
      reason_text: string;
      /** Resolved At */
      resolved_at: string;
      resolved_by: components['schemas']['ClassifiedByOut'];
    };
    /** ReviewResolutionOut */
    ReviewResolutionOut: {
      /**
       * Detection Id
       * Format: uuid
       */
      detection_id: string;
      /**
       * Resolution Id
       * Format: uuid
       */
      resolution_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Outcome */
      outcome: string;
      /** Reason Category */
      reason_category: string;
      /** Lifecycle State */
      lifecycle_state: string;
      /** Resolved At */
      resolved_at: string;
    };
    /** RevocationBody */
    RevocationBody: {
      /** Reason Es */
      reason_es: string;
    };
    /**
     * RevokedBySide
     * @enum {string}
     */
    RevokedBySide: 'client' | 'provider';
    /**
     * Role
     * @enum {string}
     */
    Role:
      | 'coordinator_sst'
      | 'line_manager'
      | 'plant_manager'
      | 'administrator'
      | 'provider_installer'
      | 'copasst'
      | 'platform_operator';
    /** SampleItemOut */
    SampleItemOut: {
      /**
       * Sample Item Id
       * Format: uuid
       */
      sample_item_id: string;
      /**
       * Classification Id
       * Format: uuid
       */
      classification_id: string;
      /**
       * Subject Id
       * Format: uuid
       */
      subject_id: string;
      /** Classification Outcome */
      classification_outcome: string;
      /** Reason Category */
      reason_category: string;
      /** Classified By Role */
      classified_by_role: string | null;
      /** Classified At */
      classified_at: string;
      /** Outcome */
      outcome: string | null;
      /** Comment */
      comment: string | null;
      /** Reviewed By */
      reviewed_by: {
        [key: string]: unknown;
      } | null;
      /** Reviewed At */
      reviewed_at: string | null;
    };
    /**
     * SampleResultBody
     * @description El resultado de un elemento de la muestra.
     *
     *     Admite campos de más **solo para reconocerlos**: si nombran la clasificación auditada, el
     *     rechazo es ``sample_cannot_modify_classification`` (BR-LAZ-107); si no, ``invalid_request``.
     *     Nada de lo que no sea ``outcome`` o ``comment`` llega nunca al registro.
     */
    SampleResultBody: {
      /** Outcome */
      outcome?: string | null;
      /** Comment */
      comment?: string | null;
    };
    /** SampleResultOut */
    SampleResultOut: {
      /**
       * Sample Item Id
       * Format: uuid
       */
      sample_item_id: string;
      /**
       * Sample Id
       * Format: uuid
       */
      sample_id: string;
      /**
       * Package Id
       * Format: uuid
       */
      package_id: string;
      /**
       * Classification Id
       * Format: uuid
       */
      classification_id: string;
      /** Outcome */
      outcome: string;
      /** Comment */
      comment: string | null;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Reviewed At */
      reviewed_at: string;
      /** Review Completed */
      review_completed: boolean;
    };
    /**
     * ScopeLevel
     * @enum {string}
     */
    ScopeLevel: 'organization' | 'plant' | 'zone';
    /** ScopeOut */
    ScopeOut: {
      /** Plant Id */
      plant_id: string | null;
      /** Zone Id */
      zone_id: string | null;
      /** Node Id */
      node_id?: string | null;
    };
    /** ScopeRecordBody */
    ScopeRecordBody: {
      /** Scope Text Es */
      scope_text_es: string;
      /** Cameras */
      cameras: components['schemas']['CameraFramingBody'][];
      blur_verification?: components['schemas']['BlurVerificationBody'] | null;
      /** Document Ref */
      document_ref?: {
        [key: string]: unknown;
      } | null;
    };
    /** ScopeRecordOut */
    ScopeRecordOut: {
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Plant Policy Loaded At Signing */
      plant_policy_loaded_at_signing: boolean;
      gates: components['schemas']['GateStateOut'];
    };
    /** SecondFactorRequest */
    SecondFactorRequest: {
      /** Code */
      code: string;
    };
    /** SecondFactorResetView */
    SecondFactorResetView: {
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      /** Sessions Closed */
      sessions_closed: number;
    };
    /** SemVer */
    SemVer: string;
    /**
     * SessionView
     * @description Una sesión activa de la persona (sin identificador ni hash).
     */
    SessionView: {
      /** Created At */
      created_at: string;
      /** Last Seen At */
      last_seen_at: string;
      /** Client Hint */
      client_hint: string | null;
      /** Current */
      current: boolean;
      /** Usable */
      usable: boolean;
    };
    /** SessionsClosedResponse */
    SessionsClosedResponse: {
      /** Sessions Closed */
      sessions_closed: number;
    };
    /** SessionsResponse */
    SessionsResponse: {
      /** Sessions */
      sessions: components['schemas']['SessionView'][];
    };
    /** SettingsBody */
    SettingsBody: {
      /** Name */
      name?: string | null;
      /** Concession Max Days */
      concession_max_days?: number | null;
      /** Concession Default Days */
      concession_default_days?: number | null;
    };
    /** SettingsView */
    SettingsView: {
      /**
       * Organization Id
       * Format: uuid
       */
      organization_id: string;
      /** Code */
      code: string;
      /** Name */
      name: string;
      /** Kind */
      kind: string;
      /** Status */
      status: string;
      /** Concession Max Days */
      concession_max_days: number;
      /** Concession Default Days */
      concession_default_days: number;
    };
    /** Sha256Hex */
    Sha256Hex: string;
    /**
     * SignalDeclaration
     * @description Señal declarada de la zona. El nodo convierte el nivel físico leído en `asserted` o `deasserted` comparándolo con `asserted_level`.
     */
    SignalDeclaration: {
      signal_id: components['schemas']['UUID'];
      code: components['schemas']['Code'];
      role: components['schemas']['SignalRole'];
      asserted_level: components['schemas']['SignalLevel'];
      source: components['schemas']['SignalSource'];
      /** Description Es */
      description_es: string;
    };
    /**
     * SignalLevel
     * @description Nivel físico de una señal.
     * @enum {string}
     */
    SignalLevel: 'high' | 'low';
    /** SignalReaderView */
    SignalReaderView: {
      /** Available */
      available: boolean;
      /** Adapter */
      adapter: string;
    };
    /**
     * SignalRole
     * @description Rol de una señal declarada en el catálogo de la zona.
     * @enum {string}
     */
    SignalRole: 'energy' | 'guard' | 'start_command' | 'auxiliary';
    /**
     * SignalSource
     * @description Lector y canal de los que el nodo toma la señal.
     */
    SignalSource: {
      /** Reader */
      reader: string;
      /** Channel */
      channel: number;
    };
    /** SignalsBody */
    SignalsBody: {
      /** Signals */
      signals: {
        [key: string]: unknown;
      }[];
      /** Reason Es */
      reason_es: string;
    };
    /** SignatoryBody */
    SignatoryBody: {
      role: components['schemas']['Role'];
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
    };
    /** SignatoryOut */
    SignatoryOut: {
      role: components['schemas']['Role'];
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      /** Display Name */
      display_name: string | null;
      /** Confirmed At */
      confirmed_at: string | null;
    };
    /** SignatoryPolicyBody */
    SignatoryPolicyBody: {
      /** Required Roles */
      required_roles: components['schemas']['Role'][];
      /** Minimum */
      minimum: number;
    };
    /**
     * SignatoryPolicyOut
     * @description ``PlantSignatoryPolicy`` (DE §2.7); ``configured = false`` en una planta sin ella.
     */
    SignatoryPolicyOut: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Configured */
      configured: boolean;
      /** Required Roles */
      required_roles?: components['schemas']['Role'][] | null;
      /** Minimum */
      minimum?: number | null;
      /** @default copasst */
      workers_role: components['schemas']['Role'];
      /** Updated By */
      updated_by?: string | null;
      /** Updated At */
      updated_at?: string | null;
    };
    /** SignatureBody */
    SignatureBody: {
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
    };
    /** SignatureOut */
    SignatureOut: {
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      /** Role In Use */
      role_in_use: string;
      /** Signed At */
      signed_at: string;
    };
    /** SignedGateState */
    SignedGateState: components['schemas']['vigia_contracts__models__gate_state__SignedEnvelope'];
    /** SignedPublicKeySet */
    SignedPublicKeySet: components['schemas']['vigia_contracts__models__public_key_set__SignedEnvelope'];
    /** SignedZoneCatalog */
    SignedZoneCatalog: components['schemas']['vigia_contracts__models__zone_catalog__SignedEnvelope'];
    /** SignerOut */
    SignerOut: {
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      /** Display Name */
      display_name: string;
      /** Professional License */
      professional_license: string;
    };
    /**
     * SigningPurpose
     * @description ``signing_purpose`` (``domain-entities.md`` §1).
     * @enum {string}
     */
    SigningPurpose: 'catalog' | 'gate' | 'live_view_token' | 'key_set' | 'checkpoint';
    /** SingleOccupancyBody */
    SingleOccupancyBody: {
      /** Single Occupancy */
      single_occupancy: boolean;
      /**
       * Aggregation Window Minutes
       * @default 60
       */
      aggregation_window_minutes: number;
      /** Reason Es */
      reason_es: string;
    };
    /** StandardBody */
    StandardBody: {
      family: components['schemas']['PredicateFamily'];
      /** Title Es */
      title_es: string;
      /** Declared Text */
      declared_text: string;
      /** Predicate */
      predicate: {
        [key: string]: unknown;
      };
      /** Reason Es */
      reason_es: string;
      zone_parameters?: components['schemas']['ZoneParametersBody'] | null;
    };
    /** StandardChanges */
    StandardChanges: {
      /** Title Es */
      title_es?: string | null;
      /** Declared Text */
      declared_text?: string | null;
      /** Predicate */
      predicate?: {
        [key: string]: unknown;
      } | null;
    };
    /** StandardCitationOut */
    StandardCitationOut: {
      /**
       * Standard Id
       * Format: uuid
       */
      standard_id: string;
      /** Version */
      version: number;
      /** Title Es */
      title_es: string | null;
      /** Declared Text */
      declared_text: string | null;
      /** Declared Text Sha256 */
      declared_text_sha256: string | null;
      /** Effective From */
      effective_from: string | null;
      /** Superseded Since */
      superseded_since: string | null;
      /** Superseded In Catalog Version */
      superseded_in_catalog_version: number | null;
    };
    /** StandardOut */
    StandardOut: {
      /**
       * Standard Id
       * Format: uuid
       */
      standard_id: string;
      /** Version */
      version: number;
      /** Title Es */
      title_es: string;
      /** Declared Text */
      declared_text: string;
    };
    /** StandardVersionBody */
    StandardVersionBody: {
      changes: components['schemas']['StandardChanges'];
      /** Reason Es */
      reason_es: string;
    };
    /**
     * StartCommandAsserted
     * @description Condición `{signal_role: start_command, value: asserted}`: mando de arranque activo.
     */
    StartCommandAsserted: {
      /**
       * condition_signal_role
       * @constant
       */
      signal_role: 'start_command';
      /**
       * condition_signal_value
       * @constant
       */
      value: 'asserted';
    };
    /**
     * StartupTransitionPredicate
     * @description Plantilla `startup_transition`: `all_of` contiene presencia y mando de arranque afirmado y, opcionalmente, energía no afirmada (arranque desde reposo); en cualquier orden y sin repetir; `min_duration_ms` entre 0 y 60 000.
     */
    StartupTransitionPredicate: {
      /** All Of */
      all_of: (
        | components['schemas']['PresenceCondition']
        | components['schemas']['StartCommandAsserted']
        | components['schemas']['EnergyDeasserted']
      )[];
      /** Min Duration Ms */
      min_duration_ms: number;
    };
    /** StepBody */
    StepBody: {
      step_kind: components['schemas']['StepKind'];
      /**
       * Responsible User Id
       * Format: uuid
       */
      responsible_user_id: string;
    };
    /**
     * StepKind
     * @description Paso cronometrado del comisionamiento (ocho valores).
     * @enum {string}
     */
    StepKind:
      | 'physical_setup'
      | 'signal_mapping'
      | 'framing'
      | 'walk_test_passes'
      | 'occlusion_test'
      | 'latency_measurement'
      | 'review_and_signatures'
      | 'other';
    /**
     * StepKindDurationOut
     * @description Duración de un tipo de paso, sin responsable (H-53).
     */
    StepKindDurationOut: {
      step_kind: components['schemas']['StepKind'];
      /** Duration Ms */
      duration_ms: number;
    };
    /**
     * StepOut
     * @description Un paso con sus marcas del servidor y, aparte, su corrección (BR-GOB-45).
     */
    StepOut: {
      /**
       * Step Id
       * Format: uuid
       */
      step_id: string;
      /**
       * Session Id
       * Format: uuid
       */
      session_id: string;
      step_kind: components['schemas']['StepKind'];
      /**
       * Responsible User Id
       * Format: uuid
       */
      responsible_user_id: string;
      /** Started At */
      started_at: string;
      /** Ended At */
      ended_at: string | null;
      correction: components['schemas']['CorrectionOut'] | null;
      /** Effective Duration Ms */
      effective_duration_ms: number | null;
    };
    StorageKey: string;
    /** SubjectOut */
    SubjectOut: {
      /** Kind */
      kind: string;
      /** Camera Id */
      camera_id?: string | null;
      /** Signal Id */
      signal_id?: string | null;
    };
    /** SummaryOut */
    SummaryOut: {
      /** Observable Ms */
      observable_ms: number;
      /** Degraded Ms */
      degraded_ms: number;
      /** Not Observable Ms */
      not_observable_ms: number;
      /** No Communication Ms */
      no_communication_ms: number;
      /** Never Reported Ms */
      never_reported_ms: number;
      /** Zone Not Active Ms */
      zone_not_active_ms: number;
    };
    /** TargetVersionBody */
    TargetVersionBody: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Node Ids */
      node_ids?: string[] | null;
      group?: components['schemas']['NodeGroup'] | null;
      /** Target Version */
      target_version: string;
      maintenance_window: components['schemas']['MaintenanceWindowBody'];
    };
    /** TargetVersionPublicationView */
    TargetVersionPublicationView: {
      /**
       * Publication Id
       * Format: uuid
       */
      publication_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Target Version */
      target_version: string;
      /** Node Ids */
      node_ids: string[];
      maintenance_window: components['schemas']['MaintenanceWindowView'];
      /**
       * Published By
       * Format: uuid
       */
      published_by: string;
      /** Published At */
      published_at: string;
      /**
       * Ledger Record Id
       * Format: uuid
       */
      ledger_record_id: string;
    };
    /** TechnicalId */
    TechnicalId: string;
    /**
     * TermBody
     * @description Un término que se añade.
     */
    TermBody: {
      /** Category */
      category: string;
      /** Form */
      form: string;
      /** Value */
      value: string;
      /** Suggestion Es */
      suggestion_es: string;
    };
    /** TermOut */
    TermOut: {
      /**
       * Term Id
       * Format: uuid
       */
      term_id: string;
      /** Category */
      category: string;
      /** Form */
      form: string;
      /** Value */
      value: string;
      /** Lemma */
      lemma: string | null;
      /** Suggestion Es */
      suggestion_es: string;
      /** Origin */
      origin: string;
      /** Added In Version */
      added_in_version: number;
    };
    /** ThresholdValues */
    ThresholdValues: {
      /** Review */
      review: number;
      /** Publication */
      publication: number;
    };
    /**
     * Thresholds
     * @description Umbrales de confianza: `0 < review < publication ≤ 1`. El esquema acota cada umbral; la comparación `review < publication` la verifica el modelo.
     */
    Thresholds: {
      /** Review */
      review: number;
      /** Publication */
      publication: number;
    };
    /** ThresholdsBody */
    ThresholdsBody: {
      /** Review */
      review: number;
      /** Publication */
      publication: number;
      /** Reason Es */
      reason_es: string;
    };
    TimeSource: string;
    /** Timestamp */
    Timestamp: string;
    /** TrancheOut */
    TrancheOut: {
      /** Median Ms */
      median_ms?: number | null;
      /** P95 Ms */
      p95_ms: number;
      /** Max Ms */
      max_ms?: number | null;
      /** Repetitions */
      repetitions?: number | null;
      /** Measured By */
      measured_by: string;
    };
    /** TransparencyOut */
    TransparencyOut: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      declared_scope: components['schemas']['DeclaredScopeOut'];
      /** Standards */
      standards: components['schemas']['StandardOut'][];
      current_agreement: components['schemas']['AgreementOut'] | null;
      gates: components['schemas']['GatesOut'];
      /** Pending Confirmation For Me */
      pending_confirmation_for_me: string | null;
    };
    /** UUID */
    UUID: string;
    /** UUIDv7 */
    UUIDv7: string;
    /**
     * UnclosableDeclarationBody
     * @description Los cinco campos, la fecha de revisión y la licencia del firmante (SUP-U05-12).
     */
    UnclosableDeclarationBody: {
      /** Evaluated Alternative */
      evaluated_alternative?: string | null;
      /** Rejection Reason */
      rejection_reason?: string | null;
      /** Responsible User Id */
      responsible_user_id?: string | null;
      /** Decision Date */
      decision_date?: string | null;
      /** Compensatory Measure */
      compensatory_measure?: string | null;
      /** Review Due At */
      review_due_at?: string | null;
      /** Professional License */
      professional_license?: string | null;
    };
    /** UnclosableDeclarationOut */
    UnclosableDeclarationOut: {
      /**
       * Finding State Id
       * Format: uuid
       */
      finding_state_id: string;
      /**
       * Declaration Id
       * Format: uuid
       */
      declaration_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Review Due At */
      review_due_at: string;
      /**
       * Policy Id
       * Format: uuid
       */
      policy_id: string;
      /** Policy Version */
      policy_version: number;
      /** Policy Document Sha256 */
      policy_document_sha256: string;
      /** Lifecycle State */
      lifecycle_state: string;
      /** Declared At */
      declared_at: string;
      /** Time Aggregated */
      time_aggregated: boolean;
    };
    /**
     * UnclosableReviewBody
     * @description La revisión: renovar la declaración (T11) o convertirla en acción (T12).
     */
    UnclosableReviewBody: {
      /** Outcome */
      outcome?: string | null;
      /** Review Note */
      review_note?: string | null;
      /** New Review Due At */
      new_review_due_at?: string | null;
      action?: components['schemas']['ReviewActionBody'] | null;
      /** Professional License */
      professional_license?: string | null;
    };
    /** UnclosableReviewOut */
    UnclosableReviewOut: {
      /**
       * Declaration Id
       * Format: uuid
       */
      declaration_id: string;
      /**
       * Finding State Id
       * Format: uuid
       */
      finding_state_id: string;
      /**
       * Review Id
       * Format: uuid
       */
      review_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** Outcome */
      outcome: string;
      /** New Review Due At */
      new_review_due_at: string | null;
      /** New Action Id */
      new_action_id: string | null;
      /** Lifecycle State */
      lifecycle_state: string;
      /** Reviewed At */
      reviewed_at: string;
      /** Time Aggregated */
      time_aggregated: boolean;
    };
    /**
     * UploadGrantRequest
     * @description El tipo y el tamaño se comprueban después, con su ``detail_code`` (SUP-U05-09).
     */
    UploadGrantRequest: {
      /** Content Type */
      content_type: string;
      /** Size Bytes */
      size_bytes: number;
      /** Sha256 */
      sha256: string;
    };
    /** UploadGrantResponse */
    UploadGrantResponse: {
      /**
       * Attachment Id
       * Format: uuid
       */
      attachment_id: string;
      /** Storage Key */
      storage_key: string;
      upload: components['schemas']['UploadRequest'];
    };
    /** UploadRequest */
    UploadRequest: {
      /**
       * Method
       * @constant
       */
      method: 'PUT';
      /** Url */
      url: string;
      /** Required Headers */
      required_headers: {
        [key: string]: string;
      };
      /** Expires At */
      expires_at: string;
    };
    /**
     * UsageGate
     * @description Compuerta de uso de la zona (acuerdo a tres firmas, P9).
     */
    UsageGate: {
      status: components['schemas']['GateStatus'];
      decided_at?: components['schemas']['Timestamp'] | null;
      agreement_id?: components['schemas']['UUID'] | null;
    };
    /** UsageGateOut */
    UsageGateOut: {
      status: components['schemas']['GateStatus'];
      /** Decided At */
      decided_at: string | null;
      /** Agreement Id */
      agreement_id: string | null;
      /** Decided By */
      decided_by: string | null;
    };
    /** UseAgreementBody */
    UseAgreementBody: {
      /** Signatories */
      signatories: components['schemas']['SignatoryBody'][];
      /** Document Ref */
      document_ref?: {
        [key: string]: unknown;
      } | null;
      /** Replaces Agreement Id */
      replaces_agreement_id?: string | null;
    };
    /** UserAssignmentView */
    UserAssignmentView: {
      /**
       * Assignment Id
       * Format: uuid
       */
      assignment_id: string;
      role: components['schemas']['Role'];
      scope_level: components['schemas']['ScopeLevel'];
      /**
       * Scope Id
       * Format: uuid
       */
      scope_id: string;
    };
    /** UserView */
    UserView: {
      /**
       * User Id
       * Format: uuid
       */
      user_id: string;
      /** Display Name */
      display_name: string;
      /** Email */
      email: string;
    };
    /** UsersPage */
    UsersPage: {
      /** Users */
      users: components['schemas']['ManagedUserView'][];
      /** Next After */
      next_after: string | null;
    };
    /** VerificationAccepted */
    VerificationAccepted: {
      /**
       * Request Id
       * Format: uuid
       */
      request_id: string;
      chain: components['schemas']['ChainOut'];
      /**
       * Status
       * @constant
       */
      status: 'queued';
      /** Requested At */
      requested_at: string;
    };
    /**
     * VerificationClipReceipt
     * @description Recibo de un clip de verificación del difuminado, respuesta de `POST clip-uploads/{clip_id}/confirmation` (pendiente nº 32, domain-entities.md §4). Solo existe para concesiones con `purpose = verification`: el nodo sube en `commissioning` un clip difuminado de 10 s sin hallazgo y U-03 lo guarda como `VerificationClip` de la zona. Repetir la confirmación devuelve el mismo recibo.
     */
    VerificationClipReceipt: {
      clip_id: components['schemas']['UUIDv7'];
      zone_id: components['schemas']['UUID'];
      node_id: components['schemas']['UUID'];
      received_at: components['schemas']['Timestamp'];
      sha256: components['schemas']['Sha256Hex'];
    };
    /** VerifierOut */
    VerifierOut: {
      /** Name */
      name: string;
      /** Sha256 */
      sha256: string;
      /** Size Bytes */
      size_bytes: number;
      /** Format Version */
      format_version: number;
    };
    /**
     * VerifyRequest
     * @description La cadena a verificar: ``ledger`` con ``plant_id`` (planta) o sin él (organización), o
     *     ``audit`` (sin planta). Sin campos de más; ``plant_id`` es un UUID en texto.
     */
    VerifyRequest: {
      /**
       * Kind
       * @enum {string}
       */
      kind: 'ledger' | 'audit';
      /** Plant Id */
      plant_id?: string | null;
    };
    /**
     * VocabularyChangeBody
     * @description Lo que se añade y se retira, y el motivo (obligatorio).
     */
    VocabularyChangeBody: {
      /**
       * Added
       * @default []
       */
      added: components['schemas']['TermBody'][];
      /**
       * Removed
       * @default []
       */
      removed: string[];
      /**
       * Declared Names Added
       * @default []
       */
      declared_names_added: components['schemas']['DeclaredNameBody'][];
      /**
       * Declared Names Removed
       * @default []
       */
      declared_names_removed: string[];
      /** Reason */
      reason?: string | null;
    };
    /** VocabularyChangeOut */
    VocabularyChangeOut: {
      /**
       * Change Id
       * Format: uuid
       */
      change_id: string;
      /**
       * Record Id
       * Format: uuid
       */
      record_id: string;
      /** List Version */
      list_version: number;
      /** Changed At */
      changed_at: string;
      /** Added */
      added: components['schemas']['TermOut'][];
      /** Removed */
      removed: string[];
      /** Declared Names Added */
      declared_names_added: components['schemas']['DeclaredNameOut'][];
      /** Declared Names Removed */
      declared_names_removed: string[];
    };
    /** VocabularyOut */
    VocabularyOut: {
      /** List Version */
      list_version: number;
      /** Lemmatizer Version */
      lemmatizer_version: string;
      /** Effective From */
      effective_from: string | null;
      /** Record Id */
      record_id: string | null;
      /** Terms */
      terms: components['schemas']['TermOut'][];
      /** Declared Names */
      declared_names: components['schemas']['DeclaredNameOut'][];
    };
    /** VolumeDownloadView */
    VolumeDownloadView: {
      /** Volume */
      volume: number;
      /** Name */
      name: string;
      /** Size Bytes */
      size_bytes: number;
      /** Sha256 */
      sha256: string;
      /** Url */
      url: string;
      /** Expires At */
      expires_at: string;
    };
    /**
     * WalkTestKind
     * @enum {string}
     */
    WalkTestKind: 'initial' | 'regression_rerun';
    /**
     * WalkTestSessionOut
     * @description ``WalkTestSession`` (DE §2.11) con lo registrado en ella.
     */
    WalkTestSessionOut: {
      /**
       * Session Id
       * Format: uuid
       */
      session_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /**
       * Node Id
       * Format: uuid
       */
      node_id: string;
      /** Catalog Version */
      catalog_version: number;
      kind: components['schemas']['WalkTestKind'];
      status: components['schemas']['WalkTestStatus'];
      /** Passes Per Cell */
      passes_per_cell: number;
      /** Rows */
      rows: components['schemas']['MatrixRowOut'][];
      /** Started At */
      started_at: string;
      /** Last Activity At */
      last_activity_at: string;
      /** Closed At */
      closed_at: string | null;
      /** Reopened At */
      reopened_at: string | null;
      /** Reopened By */
      reopened_by: string | null;
      /** Reopen Reason Es */
      reopen_reason_es: string | null;
      /** Steps */
      steps: components['schemas']['StepOut'][];
      /** Passes */
      passes: components['schemas']['PassOut'][];
      /** Total Duration Ms */
      total_duration_ms: number;
      /** Steps Summary */
      steps_summary: components['schemas']['StepKindDurationOut'][];
      /** Occlusion Tests */
      occlusion_tests: {
        [key: string]: unknown;
      }[];
    };
    /**
     * WalkTestStatus
     * @enum {string}
     */
    WalkTestStatus: 'in_progress' | 'closed' | 'incomplete' | 'reopened';
    /** WhyStepOut */
    WhyStepOut: {
      /** Step Index */
      step_index: number;
      /** Question Es */
      question_es: string;
      /** Statement */
      statement: string;
      /** Evidence Refs */
      evidence_refs: components['schemas']['EvidenceRefOut'][];
      /** Category */
      category: string | null;
    };
    /** WindowsBody */
    WindowsBody: {
      clip_window?: components['schemas']['ClipWindowBody'] | null;
      episode?: components['schemas']['EpisodeBody'] | null;
      /** Reason Es */
      reason_es: string;
    };
    /** ZoneBody */
    ZoneBody: {
      /** Code */
      code: string;
      /** Name */
      name: string;
    };
    /**
     * ZoneContextOut
     * @description ``ZoneObjectiveContext`` (DE §2.10): solo hechos medidos de la zona (BR-LAZ-63).
     */
    ZoneContextOut: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Family */
      family: string;
      /** Tier */
      tier: string;
      /**
       * Standard Id
       * Format: uuid
       */
      standard_id: string;
      /** Standard Version */
      standard_version: number;
      /** Standard Title Es */
      standard_title_es: string;
      /** Standard Text */
      standard_text: string;
      /** Observability State At Event */
      observability_state_at_event: string;
      period: components['schemas']['PeriodOut'];
      coverage_summary_period: components['schemas']['CoverageSummaryOut'];
      /** Node State */
      node_state: components['schemas']['NodeStateOut'][];
      /** Signals By Role */
      signals_by_role: components['schemas']['vigia_platform__loop__adapters__http__root_cause_routes__SignalOut'][];
      episode_durations: components['schemas']['EpisodeDurationsOut'];
      /** Finding Counts By Family In Period */
      finding_counts_by_family_in_period: components['schemas']['FamilyCountOut'][];
      /** Recurrences In Period */
      recurrences_in_period: number;
      /** Previous Actions */
      previous_actions: components['schemas']['PreviousActionOut'][];
      /** Previous Cause Categories */
      previous_cause_categories: components['schemas']['CauseCountOut'][];
    };
    /** ZoneDownloadOut */
    ZoneDownloadOut: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Url */
      url: string;
      /** Expires At */
      expires_at: string;
    };
    /**
     * ZoneMode
     * @description Modo de operación de una zona, derivado de sus compuertas.
     * @enum {string}
     */
    ZoneMode: 'no_capture' | 'commissioning' | 'productive';
    /** ZoneOut */
    ZoneOut: {
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      notice: components['schemas']['InboxZoneNotice'];
      /** Node Id */
      node_id: string | null;
      /** Node Code */
      node_code: string | null;
      /** Communication State */
      communication_state: string | null;
    };
    /**
     * ZoneParametersBody
     * @description Los parámetros de la versión 1 de la zona (solo con la zona aún sin catálogo).
     */
    ZoneParametersBody: {
      /** Cameras */
      cameras: components['schemas']['CameraBody'][];
      minimum_coverage: components['schemas']['CoverageValues'];
      /** Signals */
      signals: {
        [key: string]: unknown;
      }[];
      thresholds: components['schemas']['ThresholdValues'];
      clip_window: components['schemas']['ClipWindowBody'];
      episode: components['schemas']['EpisodeBody'];
      /**
       * Single Occupancy
       * @default false
       */
      single_occupancy: boolean;
      /**
       * Aggregation Window Minutes
       * @default 60
       */
      aggregation_window_minutes: number;
    };
    /** ZoneStateView */
    ZoneStateView: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** Mode */
      mode: string;
      /** Observability State */
      observability_state: string;
      /** Catalog Version */
      catalog_version: number;
      /** Coverage Ok */
      coverage_ok: boolean;
    };
    /** ZoneView */
    ZoneView: {
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /**
       * Plant Id
       * Format: uuid
       */
      plant_id: string;
      /** Code */
      code: string;
      /** Name */
      name: string;
      /** Node Id */
      node_id: string | null;
    };
    /**
     * GateState
     * @description Estado de las compuertas de una zona (domain-entities.md §3.3). Siempre viaja firmado, como `SignedEnvelope<GateState>` (`#/$defs/SignedGateState`), con una clave de propósito `gate`, y solo por `HeartbeatResponse.gate_states` y `NodeInitialConfiguration.gate_states`: no tiene ruta propia (BR-CTR-55). `resulting_mode` se deriva de las dos compuertas y el esquema exige la derivación (BR-CTR-40): montaje no aprobada → `no_capture`; montaje aprobada y uso no aprobada → `commissioning`; ambas aprobadas → `productive`; una compuerta `revoked` cuenta como no aprobada. `valid_until − issued_at` es 7 días (BR-CTR-43); JSON Schema no resta marcas, así que lo verifica el modelo. Vencido `valid_until` sin refresco, el nodo retrocede al modo inmediatamente más restrictivo (BR-CTR-42).
     */
    vigia_contracts__models__gate_state__Payload: {
      zone_id: components['schemas']['UUID'];
      organization_id: components['schemas']['UUID'];
      plant_id: components['schemas']['UUID'];
      mounting_gate: components['schemas']['MountingGate'];
      usage_gate: components['schemas']['UsageGate'];
      resulting_mode: components['schemas']['ZoneMode'];
      issued_at: components['schemas']['Timestamp'];
      valid_until: components['schemas']['Timestamp'];
    };
    /**
     * SignedEnvelope
     * @description Sobre firmado `SignedEnvelope<T>` (domain-entities.md §3.5). La firma es Ed25519 sobre la serialización canónica RFC 8785 de `payload`; el receptor verifica con la clave pública cuyo `key_id` coincide y cuyo `purpose` corresponde al tipo de carga. Un sobre que no verifica no se aplica ni se cachea. Es genérico: `payload` se resuelve por `$dynamicRef` al ancla dinámica `payload`, y quien lo usa lo especializa con `{"$ref": "signed_envelope.schema.json", "$defs": {"payload": {"$dynamicAnchor": "payload", "$ref": "<esquema de la carga>"}}}`. Sin especializar no admite ninguna carga.
     */
    vigia_contracts__models__gate_state__SignedEnvelope: {
      payload: components['schemas']['vigia_contracts__models__gate_state__Payload'];
      payload_canonical_sha256: components['schemas']['Sha256Hex'];
      /** Signature */
      signature: string;
      key_id: components['schemas']['KeyId'];
      signed_at: components['schemas']['Timestamp'];
    };
    /**
     * PublicKeySet
     * @description Conjunto vigente de claves públicas de la plataforma (nota de domain-entities.md §3.5). Viaja como `SignedEnvelope<PublicKeySet>` en `HeartbeatResponse.platform_public_keys`, firmado con una clave de propósito `key_set` que el nodo ya tiene fijada; un conjunto que no verifica no se aplica.
     */
    vigia_contracts__models__public_key_set__Payload: {
      /** Keys */
      keys: components['schemas']['PublicKey'][];
      issued_at: components['schemas']['Timestamp'];
    };
    /**
     * SignedEnvelope
     * @description Sobre firmado `SignedEnvelope<T>` (domain-entities.md §3.5). La firma es Ed25519 sobre la serialización canónica RFC 8785 de `payload`; el receptor verifica con la clave pública cuyo `key_id` coincide y cuyo `purpose` corresponde al tipo de carga. Un sobre que no verifica no se aplica ni se cachea. Es genérico: `payload` se resuelve por `$dynamicRef` al ancla dinámica `payload`, y quien lo usa lo especializa con `{"$ref": "signed_envelope.schema.json", "$defs": {"payload": {"$dynamicAnchor": "payload", "$ref": "<esquema de la carga>"}}}`. Sin especializar no admite ninguna carga.
     */
    vigia_contracts__models__public_key_set__SignedEnvelope: {
      payload: components['schemas']['vigia_contracts__models__public_key_set__Payload'];
      payload_canonical_sha256: components['schemas']['Sha256Hex'];
      /** Signature */
      signature: string;
      key_id: components['schemas']['KeyId'];
      signed_at: components['schemas']['Timestamp'];
    };
    /**
     * ZoneCatalog
     * @description Catálogo de una zona: cámaras, cobertura mínima, señales, estándares declarados y parámetros de detección (domain-entities.md §3.1). Siempre viaja firmado, como `SignedEnvelope<ZoneCatalog>` (`#/$defs/SignedZoneCatalog`), con una clave de propósito `catalog`. `version` es monótona por zona y cualquier cambio la incrementa. Reglas entre campos que JSON Schema 2020-12 no expresa y que verifican los modelos (VIG-29), la plataforma al publicar y la suite de conformidad: (1) `thresholds.review < thresholds.publication`; (2) `minimum_coverage.required_count ≤ len(cameras)`; (3) `minimum_coverage.required_count ≥ len(minimum_coverage.required_camera_ids)` (pendiente nº 34, entrada A-03 de la adenda); (4) toda cámara de `required_camera_ids` está en `cameras`; (5) `camera_id` y `code` únicos en `cameras`, `signal_id` y `code` únicos en `signals`, y un solo `standard_id` por catálogo en `standards` (una sola versión vigente). Un catálogo que viole (2) a (4) no es satisfacible: U-03 lo rechaza con `unsatisfiable_coverage` y los generadores del kit nunca lo producen.
     */
    vigia_contracts__models__zone_catalog__Payload: {
      zone_id: components['schemas']['UUID'];
      zone_code: components['schemas']['Code'];
      plant_id: components['schemas']['UUID'];
      organization_id: components['schemas']['UUID'];
      /** Version */
      version: number;
      issued_at: components['schemas']['Timestamp'];
      /** Cameras */
      cameras: components['schemas']['CatalogCamera'][];
      minimum_coverage: components['schemas']['MinimumCoverage'];
      /** Signals */
      signals: components['schemas']['SignalDeclaration'][];
      /** Standards */
      standards: components['schemas']['DeclaredStandard'][];
      thresholds: components['schemas']['Thresholds'];
      clip_window: components['schemas']['ClipWindow'];
      episode: components['schemas']['EpisodeParameters'];
      /**
       * Commissioning Watermark
       * @constant
       */
      commissioning_watermark: true;
    };
    /**
     * SignedEnvelope
     * @description Sobre firmado `SignedEnvelope<T>` (domain-entities.md §3.5). La firma es Ed25519 sobre la serialización canónica RFC 8785 de `payload`; el receptor verifica con la clave pública cuyo `key_id` coincide y cuyo `purpose` corresponde al tipo de carga. Un sobre que no verifica no se aplica ni se cachea. Es genérico: `payload` se resuelve por `$dynamicRef` al ancla dinámica `payload`, y quien lo usa lo especializa con `{"$ref": "signed_envelope.schema.json", "$defs": {"payload": {"$dynamicAnchor": "payload", "$ref": "<esquema de la carga>"}}}`. Sin especializar no admite ninguna carga.
     */
    vigia_contracts__models__zone_catalog__SignedEnvelope: {
      payload: components['schemas']['vigia_contracts__models__zone_catalog__Payload'];
      payload_canonical_sha256: components['schemas']['Sha256Hex'];
      /** Signature */
      signature: string;
      key_id: components['schemas']['KeyId'];
      signed_at: components['schemas']['Timestamp'];
    };
    /** AnalysisOut */
    vigia_platform__loop__adapters__http__findings_routes__AnalysisOut: {
      /**
       * Analysis Id
       * Format: uuid
       */
      analysis_id: string;
      /** Record Id */
      record_id: string | null;
      /** State */
      state: string;
      /** Root Cause Category */
      root_cause_category: string | null;
      /** Root Cause Statement */
      root_cause_statement: string | null;
      /** Conclusion */
      conclusion: string | null;
      /** Signed At */
      signed_at: string | null;
      /** Created At */
      created_at: string;
    };
    /** SignalOut */
    vigia_platform__loop__adapters__http__findings_routes__SignalOut: {
      /** Signal Id */
      signal_id: string | null;
      /** Role */
      role: string;
      /** Value */
      value: string;
      /** Valid */
      valid: boolean;
      /** Read At */
      read_at: string | null;
    };
    /** AnalysisOut */
    vigia_platform__loop__adapters__http__root_cause_routes__AnalysisOut: {
      /**
       * Analysis Id
       * Format: uuid
       */
      analysis_id: string;
      /** Subject Kind */
      subject_kind: string;
      /**
       * Subject Id
       * Format: uuid
       */
      subject_id: string;
      /**
       * Zone Id
       * Format: uuid
       */
      zone_id: string;
      /** State */
      state: string;
      zone_context: components['schemas']['ZoneContextOut'];
      /** Why Steps */
      why_steps: components['schemas']['WhyStepOut'][];
      /** Next Step Index */
      next_step_index: number | null;
      /** Next Question Es */
      next_question_es: string | null;
      /** Cause Effect Branches */
      cause_effect_branches: components['schemas']['BranchOut'][];
      /** Root Cause Category */
      root_cause_category: string | null;
      /** Root Cause Statement */
      root_cause_statement: string | null;
      /** Conclusion */
      conclusion: string | null;
      /** Pending Information */
      pending_information: string[];
      /** Missing Information */
      missing_information: string | null;
      /** Record Id */
      record_id: string | null;
      signer: components['schemas']['SignerOut'] | null;
      /** Signed At */
      signed_at: string | null;
      /** Created At */
      created_at: string;
      /** Updated At */
      updated_at: string;
    };
    /** SignalOut */
    vigia_platform__loop__adapters__http__root_cause_routes__SignalOut: {
      /** Role */
      role: string;
      /** Value */
      value: string;
      /** Valid */
      valid: boolean;
    };
  };
  responses: never;
  parameters: never;
  requestBodies: never;
  headers: never;
  pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
  live_health_live_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['LiveStatus'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  checkpoint_keys__well_known_vigia_checkpoint_keys_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CheckpointKeysOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  verifier__well_known_vigia_verifier_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['VerifierOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  replay_platform_dead_letter__event_id___consumer__replay_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        event_id: string;
        consumer: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ReplayOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  rotate_platform_keys__purpose__rotate_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        purpose: components['schemas']['SigningPurpose'];
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['KeyRotatedOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  login_auth_login_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['LoginRequest'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['LoginResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  second_factor_auth_second_factor_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['SecondFactorRequest'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['LoginResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  enroll_auth_second_factor_enroll_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['EnrollRequest'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['EnrollResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  logout_auth_logout_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      204: {
        headers: {
          [name: string]: unknown;
        };
        content?: never;
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  sessions_auth_sessions_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['SessionsResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  close_others_auth_sessions_close_others_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['SessionsClosedResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  change_password_auth_password_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['PasswordChangeRequest'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['SessionsClosedResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  accept_invitations__token__accept_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        token: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json':
          components['schemas']['BeginActivation'] | components['schemas']['CompleteActivation'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ActivationResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  me_me_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['MeResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  accept_notice_privacy_notice_accept_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['NoticeAcceptRequest'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NoticeAcceptResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_users_users_get: {
    parameters: {
      query?: {
        after?: string | null;
        limit?: number;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['UsersPage'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  invite_users_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['InviteBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['InvitationView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  deactivate_users__user_id__deactivate_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        user_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['DeactivatedView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  reactivate_users__user_id__reactivate_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        user_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ReactivateBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['InvitationView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  update_profile_users__user_id__patch: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        user_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ProfileBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      204: {
        headers: {
          [name: string]: unknown;
        };
        content?: never;
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  reset_second_factor_users__user_id__second_factor_reset_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        user_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['SecondFactorResetView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  assign_role_users__user_id__roles_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        user_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['AssignmentBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['AssignedView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  remove_role_users__user_id__roles__assignment_id__delete: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        user_id: string;
        assignment_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      204: {
        headers: {
          [name: string]: unknown;
        };
        content?: never;
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  hierarchy_hierarchy_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['HierarchyView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  create_plant_plants_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['PlantBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['PlantView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  create_zone_plants__plant_id__zones_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ZoneBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ZoneView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  read_settings_organization_settings_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['SettingsView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  update_settings_organization_settings_patch: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['SettingsBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['SettingsView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_concessions_concessions_get: {
    parameters: {
      query?: {
        plant_id?: string | null;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ConcessionsView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_queries_concessions__concession_id__queries_get: {
    parameters: {
      query?: {
        after?: string | null;
        limit?: number;
      };
      header?: never;
      path: {
        concession_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ProviderQueriesPage'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  revoke_concessions__concession_id__revoke_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        concession_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ConcessionView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  own_concessions_provider_concessions_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ProviderConcessionsView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  grant_provider_concessions_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['GrantBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ProviderConcessionView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_records_ledger_records_get: {
    parameters: {
      query?: {
        record_type?: string[] | null;
        plant_id?: string | null;
        zone_id?: string | null;
        node_id?: string | null;
        received_from?: string | null;
        received_before?: string | null;
        page_size?: number;
        after_received_at?: string | null;
        after_record_id?: string | null;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['LedgerRecordPage'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  get_record_ledger_records__record_id__get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        record_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['LedgerRecordOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_audit_entries_audit_entries_get: {
    parameters: {
      query?: {
        operation?: string[] | null;
        actor_id?: string | null;
        plant_id?: string | null;
        zone_id?: string | null;
        occurred_from?: string | null;
        occurred_before?: string | null;
        page_size?: number;
        after_occurred_at?: string | null;
        after_entry_id?: string | null;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['AuditEntryPage'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  read_url_evidence__evidence_id__read_url_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        evidence_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['EvidenceReadUrlResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_labels_labels_get: {
    parameters: {
      query: {
        from: string;
        to: string;
        zone_id?: string | null;
        family?: string | null;
        reason_category?: string | null;
        page_size?: number;
        after_labeled_at?: string | null;
        after_label_id?: string | null;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['LabelPageOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  timeline_zones__zone_id__coverage_get: {
    parameters: {
      query: {
        from: string;
        to: string;
      };
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CoverageTimelineOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  status_at_zones__zone_id__coverage_at_get: {
    parameters: {
      query: {
        instant: string;
      };
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CoverageStatusOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  verify_integrity_verify_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['VerifyRequest'];
      };
    };
    responses: {
      /** @description Successful Response */
      202: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['VerificationAccepted'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  results_integrity_results_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['IntegrityResultsOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  checkpoints_integrity_checkpoints_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CheckpointsOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  live_view_token_zones__zone_id__live_view_token_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['LiveViewTokenOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_admissions_plants__plant_id__admissions_get: {
    parameters: {
      query?: {
        after?: string | null;
        limit?: number;
      };
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['AdmissionsPage'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  evaluate_plants__plant_id__admissions_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['AdmissionBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['AdmissionView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  grant_document_documents_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['DocumentGrantBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['DocumentGrantOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  gate_state_zones__zone_id__gates_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['GateStateOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  scope_record_zones__zone_id__gates_mounting_scope_record_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ScopeRecordBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ScopeRecordOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  revocation_zones__zone_id__gates__gate__revocation_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
        gate: components['schemas']['GateKind'];
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['RevocationBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['GateStateOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  plant_policy_plants__plant_id__policy_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['PlantPolicyOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  sign_policy_plants__plant_id__policy_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['PlantPolicyBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['PlantPolicyOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  current_catalog_zones__zone_id__catalog_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogVersionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  catalog_history_zones__zone_id__catalog_versions_get: {
    parameters: {
      query?: {
        after?: string | null;
        limit?: number;
      };
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogHistoryOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  catalog_version_zones__zone_id__catalog_versions__catalog_version__get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
        catalog_version: number;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogVersionDetail'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  single_occupancy_zones__zone_id__catalog_single_occupancy_put: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['SingleOccupancyBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogVersionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  new_standard_zones__zone_id__standards_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['StandardBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogVersionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  new_standard_version_zones__zone_id__standards__standard_id__versions_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
        standard_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['StandardVersionBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogVersionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  retirement_zones__zone_id__standards__standard_id__retirement_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
        standard_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['RetirementBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogVersionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  cameras_zones__zone_id__cameras_put: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['CamerasBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogVersionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  minimum_coverage_zones__zone_id__minimum_coverage_put: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['MinimumCoverageBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogVersionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  signals_zones__zone_id__signals_put: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['SignalsBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogVersionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  thresholds_zones__zone_id__thresholds_put: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ThresholdsBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogVersionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  windows_zones__zone_id__windows_put: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['WindowsBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CatalogVersionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  regression_state_zones__zone_id__regression_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RegressionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  framing_recapture_zones__zone_id__framing_recaptures_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['FramingRecaptureBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RegressionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  signatory_policy_plants__plant_id__signatory_policy_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['SignatoryPolicyOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  put_signatory_policy_plants__plant_id__signatory_policy_put: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['SignatoryPolicyBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['SignatoryPolicyOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  create_agreement_zones__zone_id__use_agreements_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['UseAgreementBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['AgreementOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  confirm_agreement_use_agreements__agreement_id__confirmations_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        agreement_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ConfirmationOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  approve_agreement_use_agreements__agreement_id__approval_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        agreement_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApprovalOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  transparency_zones__zone_id__transparency_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['TransparencyOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  open_walk_test_zones__zone_id__walk_tests_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['OpenWalkTestBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['WalkTestSessionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  current_walk_test_zones__zone_id__walk_tests_current_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CurrentWalkTestOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  start_step_walk_tests__session_id__steps_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        session_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['StepBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['StepOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  close_step_walk_tests__session_id__steps__step_id__close_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        session_id: string;
        step_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['CloseStepBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['StepOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  record_pass_walk_tests__session_id__passes_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        session_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['PassBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['PassOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  reopen_walk_test_walk_tests__session_id__reopen_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        session_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ReopenBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['WalkTestSessionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  record_occlusion_test_walk_tests__session_id__occlusion_tests_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        session_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['OcclusionTestBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['OcclusionTestOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  open_regression_rerun_zones__zone_id__walk_tests_regression_rerun_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['RerunBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['WalkTestSessionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  close_walk_test_walk_tests__session_id__close_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        session_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['CloseBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CommissioningRecordOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  record_exposure_sample_walk_tests__session_id__exposure_samples_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        session_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ExposureSampleBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ExposureSampleOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  commissioning_record_commissioning_records__record_id__get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        record_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CommissioningRecordOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  commissioning_record_document_commissioning_records__record_id__document_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        record_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Documento legible del acta (PDF generado a demanda, no almacenado) */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/pdf': string;
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  declare_node_plants__plant_id__nodes_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['NodeDeclarationBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['DeclaredNodeView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  assign_zone_nodes__node_id__zones_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        node_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['NodeZoneBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NodeAssignmentView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  unassign_zone_nodes__node_id__zones__zone_id__unassignment_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        node_id: string;
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['NodeReasonBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NodeAssignmentView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  issue_enrollment_code_nodes__node_id__enrollment_codes_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        node_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['EnrollmentCodeView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_enrollment_attempts_nodes__node_id__enrollment_attempts_get: {
    parameters: {
      query?: {
        after?: string | null;
        limit?: number;
      };
      header?: never;
      path: {
        node_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['EnrollmentAttemptsPage'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  revoke_node_nodes__node_id__revocation_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        node_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['NodeReasonBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NodeRevocationView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  decommission_node_nodes__node_id__decommission_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        node_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['NodeReasonBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NodeDecommissionView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  commissioning_clips_zones__zone_id__commissioning_clips_get: {
    parameters: {
      query?: {
        after?: string | null;
        limit?: number;
      };
      header?: never;
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CommissioningClipsOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_fleet_nodes_fleet_nodes_get: {
    parameters: {
      query?: {
        after?: string | null;
        limit?: number;
        plant_id?: string | null;
        communication_state?: components['schemas']['CommunicationState'] | null;
        warning?: components['schemas']['FleetAlarmKind'] | null;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NodeInventoryPage'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  fleet_node_fleet_nodes__node_id__get: {
    parameters: {
      query?: {
        history_after?: string | null;
        history_limit?: number;
      };
      header?: never;
      path: {
        node_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NodeInventoryDetail'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  plant_fleet_thresholds_plants__plant_id__fleet_thresholds_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['FleetThresholdsView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  put_plant_fleet_thresholds_plants__plant_id__fleet_thresholds_put: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['FleetThresholdsBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['FleetThresholdsView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  publish_target_version_fleet_target_versions_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['TargetVersionBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['TargetVersionPublicationView'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_fleet_alarms_fleet_alarms_get: {
    parameters: {
      query?: {
        after?: string | null;
        limit?: number;
        plant_id?: string | null;
        node_id?: string | null;
        alarm_kind?: components['schemas']['FleetAlarmKind'] | null;
        status?: components['schemas']['AlarmStatus'] | null;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['FleetAlarmPage'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  post_clip_upload: {
    parameters: {
      query?: never;
      header: {
        'X-Vigia-Contract-Version': string;
      };
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ClipUploadGrant'];
        };
      };
      /** @description Bad Request */
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unauthorized */
      401: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Forbidden */
      403: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Request Entity Too Large */
      413: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unprocessable Entity */
      422: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Too Many Requests */
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Service Unavailable */
      503: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  post_clip_upload_confirmation: {
    parameters: {
      query?: never;
      header: {
        'X-Vigia-Contract-Version': string;
      };
      path: {
        clip_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['VerificationClipReceipt'];
        };
      };
      /** @description Bad Request */
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unauthorized */
      401: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Forbidden */
      403: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unprocessable Entity */
      422: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Too Many Requests */
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Service Unavailable */
      503: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  post_credential_rotation: {
    parameters: {
      query?: never;
      header: {
        'X-Vigia-Contract-Version': string;
      };
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NodeCredential'];
        };
      };
      /** @description Bad Request */
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unauthorized */
      401: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Forbidden */
      403: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Request Entity Too Large */
      413: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unprocessable Entity */
      422: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Too Many Requests */
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Service Unavailable */
      503: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  post_detection_review: {
    parameters: {
      query?: never;
      header: {
        'X-Vigia-Contract-Version': string;
        'Idempotency-Key': string;
      };
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['Receipt'];
        };
      };
      /** @description Bad Request */
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unauthorized */
      401: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Forbidden */
      403: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Conflict */
      409: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Request Entity Too Large */
      413: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unprocessable Entity */
      422: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Too Many Requests */
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Service Unavailable */
      503: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  post_enrollment: {
    parameters: {
      query?: never;
      header: {
        'X-Vigia-Contract-Version': string;
      };
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NodeEnrollmentResponse'];
        };
      };
      /** @description Bad Request */
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unauthorized */
      401: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Request Entity Too Large */
      413: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unprocessable Entity */
      422: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Too Many Requests */
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Service Unavailable */
      503: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  post_finding: {
    parameters: {
      query?: never;
      header: {
        'X-Vigia-Contract-Version': string;
        'Idempotency-Key': string;
      };
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['Receipt'];
        };
      };
      /** @description Bad Request */
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unauthorized */
      401: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Forbidden */
      403: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Conflict */
      409: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Request Entity Too Large */
      413: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unprocessable Entity */
      422: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Too Many Requests */
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Service Unavailable */
      503: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  post_heartbeat: {
    parameters: {
      query?: never;
      header: {
        'X-Vigia-Contract-Version': string;
        'Idempotency-Key': string;
      };
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['HeartbeatResponse'];
        };
      };
      /** @description Bad Request */
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unauthorized */
      401: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Forbidden */
      403: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Request Entity Too Large */
      413: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unprocessable Entity */
      422: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Too Many Requests */
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Service Unavailable */
      503: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  post_observability_event: {
    parameters: {
      query?: never;
      header: {
        'X-Vigia-Contract-Version': string;
        'Idempotency-Key': string;
      };
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['Receipt'];
        };
      };
      /** @description Bad Request */
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unauthorized */
      401: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Forbidden */
      403: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Conflict */
      409: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Request Entity Too Large */
      413: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unprocessable Entity */
      422: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Too Many Requests */
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Service Unavailable */
      503: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  post_update_result: {
    parameters: {
      query?: never;
      header: {
        'X-Vigia-Contract-Version': string;
        'Idempotency-Key': string;
      };
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['Receipt'];
        };
      };
      /** @description Bad Request */
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unauthorized */
      401: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Forbidden */
      403: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Conflict */
      409: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Request Entity Too Large */
      413: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unprocessable Entity */
      422: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Too Many Requests */
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Service Unavailable */
      503: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  get_zone_catalog: {
    parameters: {
      query?: never;
      header: {
        'X-Vigia-Contract-Version': string;
      };
      path: {
        zone_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['vigia_contracts__models__zone_catalog__SignedEnvelope'];
        };
      };
      /** @description Bad Request */
      400: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Unauthorized */
      401: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Forbidden */
      403: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Too Many Requests */
      429: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Service Unavailable */
      503: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['RejectionResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  upload_grant_loop_actions__action_id__attachments_upload_grant_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        action_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['UploadGrantRequest'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['UploadGrantResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  read_url_loop_attachments__attachment_id__read_url_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        attachment_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ReadUrlResponse'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  configuration_loop_plants__plant_id__configuration_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ConfigurationOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  change_configuration_loop_plants__plant_id__configuration_put: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ConfigurationChangeBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ConfigurationOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_classifications_loop_findings__finding_state_id__classifications_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        finding_state_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ClassificationHistoryOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  classify_loop_findings__finding_state_id__classifications_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        finding_state_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ClassificationBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ClassificationOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_review_queue_loop_review_queue_get: {
    parameters: {
      query?: {
        cursor?: string | null;
        limit?: number;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ReviewQueuePageOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  resolve_loop_review_queue__detection_id__resolution_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        detection_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ReviewResolutionBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ReviewResolutionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  assign_loop_findings__finding_state_id__actions_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        finding_state_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['CorrectiveActionBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CorrectiveActionOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  add_evidence_loop_actions__action_id__evidence_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        action_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['EvidenceEntryBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['EvidenceEntryOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  close_loop_findings__finding_state_id__closure_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        finding_state_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ClosureBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ClosureOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  reopen_loop_findings__finding_state_id__reopening_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        finding_state_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ReopeningBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ReopeningOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  start_loop_findings__finding_state_id__root_cause_analyses_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        finding_state_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['vigia_platform__loop__adapters__http__root_cause_routes__AnalysisOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  put_step_loop_root_cause_analyses__analysis_id__steps__step_index__put: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        analysis_id: string;
        step_index: number;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['AnalysisStepBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['vigia_platform__loop__adapters__http__root_cause_routes__AnalysisOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  signature_loop_root_cause_analyses__analysis_id__signature_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        analysis_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['AnalysisSignatureBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['vigia_platform__loop__adapters__http__root_cause_routes__AnalysisOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  request_assistant_loop_root_cause_analyses__analysis_id__assistant_requests_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        analysis_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      202: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['AssistantRequestOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  assistant_status_loop_assistant_requests__request_id__get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        request_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['AssistantRequestOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_findings_loop_findings_get: {
    parameters: {
      query?: {
        zone_id?: string[] | null;
        family?: components['schemas']['PredicateFamily'] | null;
        tier?: components['schemas']['EvidenceTier'] | null;
        lifecycle_state?: string[] | null;
        from?: string | null;
        to?: string | null;
        order?: components['schemas']['InboxOrder'];
        cursor?: string | null;
        limit?: number;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['InboxPageOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  get_finding_loop_findings__finding_state_id__get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        finding_state_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['FindingDetailOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_overdue_loop_overdue_get: {
    parameters: {
      query: {
        plant_id: string;
        zone_id?: string[] | null;
        from?: string | null;
        to?: string | null;
        min_overdue_days?: number;
        cursor?: string | null;
        limit?: number;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['OverduePageOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  vocabulary_loop_vocabulary_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['VocabularyOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  change_vocabulary_loop_vocabulary_changes_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['VocabularyChangeBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['VocabularyChangeOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  declare_loop_findings__finding_state_id__unclosable_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        finding_state_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['UnclosableDeclarationBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['UnclosableDeclarationOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  review_loop_unclosable__declaration_id__reviews_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        declaration_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['UnclosableReviewBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['UnclosableReviewOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  request_export_accreditation_exports_post: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['ExportRequestBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      202: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ExportStatusOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  export_status_accreditation_exports__export_id__get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        export_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ExportStatusOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  export_download_accreditation_exports__export_id__download_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        export_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ExportDownloadOut'];
        };
      };
      /** @description Conflict */
      409: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ExportConflictOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  export_document_url_accreditation_exports__export_id__download_url_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        export_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ExportDocumentUrlOut'];
        };
      };
      /** @description Conflict */
      409: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ExportConflictOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  get_monthly_package_accreditation_monthly_packages_get: {
    parameters: {
      query: {
        zone_id: string;
        period: string;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['MonthlyPackageOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  get_monthly_package_sample_accreditation_monthly_packages__package_id__sample_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        package_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['SampleItemOut'][];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  get_monthly_package_plant_download_accreditation_monthly_packages__plant_id___period__download_get: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        plant_id: string;
        period: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['MonthlyPackageDownloadOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  record_sample_result_accreditation_sample_items__sample_item_id__result_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        sample_item_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['SampleResultBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['SampleResultOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  record_commitments_accreditation_monthly_packages__package_id__commitments_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        package_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['CommitmentsBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      201: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CommitmentsRecordedOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  update_commitment_accreditation_commitments__commitment_id__patch: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        commitment_id: string;
      };
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['CommitmentUpdateBody'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CommitmentUpdatedOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_commitments_accreditation_commitments_get: {
    parameters: {
      query: {
        zone_id: string;
        period: string;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['CommitmentOut'][];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  get_exposure_accreditation_exposure_get: {
    parameters: {
      query: {
        zone_id: string;
        period: string;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ExposureOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  get_adoption_accreditation_adoption_get: {
    parameters: {
      query: {
        from: string;
        to: string;
        plant_id?: string | null;
        zone_id?: string | null;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['AdoptionReportOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  metrics_accreditation_metrics_get: {
    parameters: {
      query: {
        period: string;
        zone_id?: string | null;
        plant_id?: string | null;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['MetricsOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  list_notifications_notifications_get: {
    parameters: {
      query?: {
        after?: string | null;
        limit?: number;
        status?: components['schemas']['NotificationStatusFilter'] | null;
        since?: string | null;
        until?: string | null;
        include_archived?: boolean;
      };
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NotificationPageOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  mark_notification_read_notifications__notification_id__read_post: {
    parameters: {
      query?: never;
      header?: never;
      path: {
        notification_id: string;
      };
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      204: {
        headers: {
          [name: string]: unknown;
        };
        content?: never;
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  read_preferences_notifications_preferences_get: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody?: never;
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NotificationPreferenceOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
  update_preferences_notifications_preferences_put: {
    parameters: {
      query?: never;
      header?: never;
      path?: never;
      cookie?: never;
    };
    requestBody: {
      content: {
        'application/json': components['schemas']['NotificationPreferenceUpdateIn'];
      };
    };
    responses: {
      /** @description Successful Response */
      200: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['NotificationPreferenceOut'];
        };
      };
      /** @description Error genérico (ApiError) */
      default: {
        headers: {
          [name: string]: unknown;
        };
        content: {
          'application/json': components['schemas']['ApiErrorBody'];
        };
      };
    };
  };
}
