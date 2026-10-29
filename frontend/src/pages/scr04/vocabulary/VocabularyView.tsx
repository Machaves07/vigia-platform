/**
 * Proyección cerrada de la lista; los términos son datos del vocabulario, no afirmaciones.
 *
 * VIG-366: un término con forma de patrón se muestra por su lema (o «Patrón de texto») y la
 * expresión regular va plegada en los datos técnicos; la lista se pagina de 20 en 20.
 */
import { Pager, usePaged } from '@/components/Pager/Pager';
import { TechnicalDetails } from '@/pages/shared/TechnicalDetails';
import { screenLabel, type Vocabulary } from '@/ports/scr04Parameters';
import { text } from '@/texts';

type Term = Vocabulary['terms'][number];

function TermValue({ term }: { readonly term: Term }) {
  if (term.form !== 'pattern') return <>{term.value}</>;
  return (
    <>
      {term.lemma ?? text('scr04.vocabulary.pattern_readable')}
      <TechnicalDetails
        summary={text('scr04.vocabulary.pattern_show')}
        items={[{ name: text('scr04.vocabulary.pattern_expression'), value: term.value }]}
      />
    </>
  );
}

export function VocabularyView({ data }: { readonly data: Vocabulary }) {
  const paged = usePaged(data.terms);
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
          {paged.visible.map((term) => (
            <tr key={term.term_id}>
              <td>
                <TermValue term={term} />
              </td>
              <td>{screenLabel('blocked_term_category', term.category)}</td>
              <td>{screenLabel('blocked_term_form', term.form)}</td>
              <td>{screenLabel('blocked_term_origin', term.origin)}</td>
              <td>{term.suggestion_es}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pager
        page={paged.page}
        total={data.terms.length}
        onPage={paged.setPage}
        label={text('scr04.vocabulary.pages')}
      />
    </section>
  );
}
