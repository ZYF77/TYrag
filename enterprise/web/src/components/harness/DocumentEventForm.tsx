import { useState, type FormEvent } from 'react';
import { HARNESS_DEFAULTS } from '../../api/v2Client';
import type { DocumentCommand, DocumentMetadata } from '../../api/v2Types';

interface DocumentEventFormProps {
  loading: boolean;
  onSubmit: (command: DocumentCommand) => void;
}

interface FormState {
  eventId: string;
  eventType: DocumentCommand['eventType'];
  tenantId: string;
  sourceSystem: string;
  externalDocumentId: string;
  sourceVersionId: string;
  sha256: string;
  fileName: string;
  mediaType: string;
  bucket: string;
  objectKey: string;
  equipmentId: string;
  fixedAssetNo: string;
  assetId: string;
  documentType: string;
  departmentId: string;
  securityLevel: string;
  businessStatus: DocumentMetadata['business_status'];
}

const initialState: FormState = {
  eventId: 'harness-event-001',
  eventType: 'upsert',
  tenantId: HARNESS_DEFAULTS.tenantId,
  sourceSystem: HARNESS_DEFAULTS.sourceSystem,
  externalDocumentId: 'HARNESS-DOC-001',
  sourceVersionId: 'v1',
  sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  fileName: 'harness-manual.pdf',
  mediaType: 'application/pdf',
  bucket: 'harness-bucket',
  objectKey: 'docs/harness-manual.pdf',
  equipmentId: 'EQ-1001',
  fixedAssetNo: 'FA-2001',
  assetId: '',
  documentType: 'manual',
  departmentId: 'maintenance',
  securityLevel: '2',
  businessStatus: 'active',
};

function Field({
  label,
  value,
  onChange,
  type = 'text',
  required = true,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  required?: boolean;
}) {
  return (
    <label className="diag-field">
      <span>{label}</span>
      <input
        value={value}
        type={type}
        required={required}
        onChange={(event) => onChange(event.target.value)}
        className="diag-input"
      />
    </label>
  );
}

export function DocumentEventForm({ loading, onSubmit }: DocumentEventFormProps) {
  const [state, setState] = useState<FormState>(initialState);
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setState((previous) => ({ ...previous, [key]: value }));
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const metadata: DocumentMetadata = {
      schema_version: 1,
      tenant_id: state.tenantId,
      source_system: state.sourceSystem,
      external_document_id: state.externalDocumentId,
      equipment_id: state.equipmentId,
      fixed_asset_no: state.fixedAssetNo || null,
      asset_id: state.assetId || null,
      document_type: state.documentType,
      document_version: state.sourceVersionId,
      department_id: state.departmentId,
      security_level: Number(state.securityLevel),
      business_status: state.businessStatus,
    };
    onSubmit({
      eventId: state.eventId,
      eventType: state.eventType,
      tenantId: state.tenantId,
      sourceSystem: state.sourceSystem,
      externalDocumentId: state.externalDocumentId,
      sourceVersionId: state.sourceVersionId,
      sha256: state.sha256,
      fileName: state.fileName,
      mediaType: state.mediaType,
      source: { bucket: state.bucket, objectKey: state.objectKey },
      metadata,
    });
  };

  return (
    <form onSubmit={submit} className="harness-stack" aria-label="文件事件提交">
      <div className="console-grid-2">
        <Field label="eventId" value={state.eventId} onChange={(value) => set('eventId', value)} />
        <label className="diag-field">
          <span>eventType</span>
          <select
            value={state.eventType}
            onChange={(event) => set('eventType', event.target.value as DocumentCommand['eventType'])}
            className="diag-select"
          >
            <option value="upsert">upsert</option>
            <option value="reindex">reindex</option>
          </select>
        </label>
        <Field label="tenantId" value={state.tenantId} onChange={(value) => set('tenantId', value)} />
        <Field label="sourceSystem" value={state.sourceSystem} onChange={(value) => set('sourceSystem', value)} />
        <Field label="externalDocumentId" value={state.externalDocumentId} onChange={(value) => set('externalDocumentId', value)} />
        <Field label="sourceVersionId" value={state.sourceVersionId} onChange={(value) => set('sourceVersionId', value)} />
        <Field label="fileName" value={state.fileName} onChange={(value) => set('fileName', value)} />
        <Field label="mediaType" value={state.mediaType} onChange={(value) => set('mediaType', value)} />
        <Field label="sha256" value={state.sha256} onChange={(value) => set('sha256', value)} />
        <Field label="source.bucket" value={state.bucket} onChange={(value) => set('bucket', value)} />
        <Field label="source.objectKey" value={state.objectKey} onChange={(value) => set('objectKey', value)} />
        <Field label="metadata.equipment_id" value={state.equipmentId} onChange={(value) => set('equipmentId', value)} />
        <Field label="metadata.fixed_asset_no" value={state.fixedAssetNo} onChange={(value) => set('fixedAssetNo', value)} required={false} />
        <Field label="metadata.asset_id" value={state.assetId} onChange={(value) => set('assetId', value)} required={false} />
        <Field label="metadata.document_type" value={state.documentType} onChange={(value) => set('documentType', value)} />
        <Field label="metadata.department_id" value={state.departmentId} onChange={(value) => set('departmentId', value)} />
        <Field label="metadata.security_level" value={state.securityLevel} onChange={(value) => set('securityLevel', value)} type="number" />
        <label className="diag-field">
          <span>metadata.business_status</span>
          <select
            value={state.businessStatus}
            onChange={(event) => set('businessStatus', event.target.value as FormState['businessStatus'])}
            className="diag-select"
          >
            <option value="active">active</option>
            <option value="review_required">review_required</option>
            <option value="superseded">superseded</option>
            <option value="disabled">disabled</option>
            <option value="deleted">deleted</option>
          </select>
        </label>
      </div>
      <p className="diag-help">
        仅提交 v2 外部文件事件；浏览器不会访问对象存储。生产 HMAC 凭据应由服务端注入，本 Harness 不保存真实密钥。
      </p>
      <button
        type="submit"
        disabled={loading}
        className="console-primary-button"
      >
        {loading ? '提交中…' : '提交文件事件'}
      </button>
    </form>
  );
}
