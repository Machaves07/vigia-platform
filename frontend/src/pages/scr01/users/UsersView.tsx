/**
 * RV-02 (usuarios): columnas de la lista de usuarios, compartidas por la `KeysetTable` de la
 * página y por la vista registrada para PR-APP-07. Nombre, correo, estado, licencia, segundo
 * factor y asignaciones con rol, nivel y alcance; nunca contraseñas, secretos TOTP ni códigos de
 * recuperación, que no viajan en `ManagedUserView`.
 */
import type { KeysetColumn } from '@/components/KeysetTable/KeysetTable';
import type { ManagedUser } from '@/ports/scr01Admin';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { assignmentText, type ScopeDirectory } from '../scopes';

export function userColumns(
  directory: ScopeDirectory,
  onManage?: (user: ManagedUser) => void,
): readonly KeysetColumn<ManagedUser>[] {
  const columns: KeysetColumn<ManagedUser>[] = [
    { id: 'name', header: text('scr01_admin.users.col_name'), cell: (row) => row.display_name },
    { id: 'email', header: text('scr01_admin.users.col_email'), cell: (row) => row.email },
    {
      id: 'status',
      header: text('scr01_admin.users.col_status'),
      cell: (row) => label('account_status', row.status),
    },
    {
      id: 'license',
      header: text('scr01_admin.users.col_license'),
      cell: (row) => row.professional_license ?? text('scr01_admin.account.no_license'),
    },
    {
      id: 'second_factor',
      header: text('scr01_admin.users.col_second_factor'),
      cell: (row) =>
        row.second_factor_enrolled
          ? text('scr01_admin.account.second_factor_enrolled')
          : text('scr01_admin.account.second_factor_not_enrolled'),
    },
    {
      id: 'assignments',
      header: text('scr01_admin.users.col_assignments'),
      cell: (row) =>
        row.assignments.length === 0 ? (
          text('scr01_admin.users.no_assignments')
        ) : (
          <ul>
            {row.assignments.map((assignment, index) => (
              <li key={index}>{assignmentText(directory, assignment)}</li>
            ))}
          </ul>
        ),
    },
  ];
  if (onManage !== undefined) {
    columns.push({
      id: 'actions',
      header: text('scr01_admin.users.col_actions'),
      cell: (row) => (
        <button
          type="button"
          aria-label={`${text('scr01_admin.users.manage')}: ${row.display_name}`}
          onClick={() => {
            onManage(row);
          }}
        >
          {text('scr01_admin.users.manage')}
        </button>
      ),
    });
  }
  return columns;
}

/** Las filas de RV-02 sin paginación, para la vista registrada (mismas celdas que la tabla). */
export function UsersRows({
  users,
  directory,
}: {
  readonly users: readonly ManagedUser[];
  readonly directory: ScopeDirectory;
}) {
  const columns = userColumns(directory, () => undefined);
  return (
    <table>
      <caption>{text('scr01_admin.users.list')}</caption>
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column.id} scope="col">
              {column.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {users.map((user, index) => (
          <tr key={index}>
            {columns.map((column) => (
              <td key={column.id}>{column.cell(user)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
