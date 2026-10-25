/** RV-07: las metapropiedades recorren las mismas proyecciones que producción. */
import { createElement } from 'react';
import { ParameterView } from '@/pages/scr04/parameters/ParameterView';
import { AdmissionView } from '@/pages/scr04/admissions/AdmissionView';
import { VocabularyView } from '@/pages/scr04/vocabulary/VocabularyView';
import type { Admission, Catalog, Vocabulary } from '@/ports/scr04Parameters';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

export default {
  parameters: {
    screen: 'SCR-04',
    view: 'RV-07',
    data: readView('CatalogVersionOut'),
    render: (data) => createElement(ParameterView, { data: data as Catalog }),
    allowedFields: ['catalog_version', 'issued_at', 'reason_es'],
  },
  admissions: {
    screen: 'SCR-04',
    view: 'RV-07',
    data: readView('AdmissionView'),
    render: (data) => createElement(AdmissionView, { data: data as Admission }),
    allowedFields: [
      'family',
      'result',
      'failed_criterion',
      'evaluated_by',
      'evaluated_at',
      'ledger_record_id',
    ],
  },
  vocabulary: {
    screen: 'SCR-04',
    view: 'RV-07',
    data: readView('VocabularyOut'),
    render: (data) => createElement(VocabularyView, { data: data as Vocabulary }),
    allowedFields: [
      'list_version',
      'terms',
      'value',
      'category',
      'form',
      'origin',
      'suggestion_es',
    ],
  },
} satisfies Record<string, ViewRegistration>;
