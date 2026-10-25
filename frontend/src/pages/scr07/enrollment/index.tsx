/** SCR-07, grupo `enrollment`: declarar nodo y código de alta con sus intentos (VIG-325). */
import { useParams } from 'react-router';
import { DeclareNode } from './DeclareNode';
import { EnrollmentCode } from './EnrollmentCode';

export default function Page() {
  const { node_id } = useParams();
  if (node_id !== undefined) return <EnrollmentCode key={node_id} nodeId={node_id} />;
  return <DeclareNode />;
}
