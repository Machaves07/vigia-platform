/** Proyección cerrada de la lista; los términos son datos del vocabulario, no afirmaciones. */
import { screenLabel, type Vocabulary } from '@/ports/scr04Parameters';
import { text } from '@/texts';

export function VocabularyView({ data }: { readonly data: Vocabulary }) {
  return (
    <section>
      <p>
        {text('scr04.vocabulary.version')}: {data.list_version}
      </p>
      <table>
        <caption>{text('scr04.vocabulary.title')}</caption>
        <thead>
          <tr>
            {['value', 'category', 'form', 'origin', 'suggestion'].map((key) => (
              <th scope="col" key={key}>
                {text(`scr04.vocabulary.${key}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.terms.map((term) => (
            <tr key={term.term_id}>
              <td>{term.value}</td>
              <td>{screenLabel('blocked_term_category', term.category)}</td>
              <td>{screenLabel('blocked_term_form', term.form)}</td>
              <td>{screenLabel('blocked_term_origin', term.origin)}</td>
              <td>{term.suggestion_es}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
