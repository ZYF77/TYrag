import { mapOverviewTotal, IIngestionSummary } from './utils';

describe('mapOverviewTotal', () => {
  it('maps nested status + doc_num to pending-parse overview fields', () => {
    const data: IIngestionSummary = {
      doc_num: 2576,
      chunk_num: 100,
      token_num: 200,
      status: {
        cancel_count: 1,
        done_count: 937,
        fail_count: 18,
        running_count: 767,
        unstart_count: 854,
      },
    };

    expect(mapOverviewTotal(data)).toEqual({
      pending: 854,
      ingested: 2576,
      pendingFailed: 0,
      processing: 767,
      finished: 937,
      failed: 18,
      cancelled: 1,
      downloaded: 854,
    });
  });

  it('defaults every field to 0 when status is missing', () => {
    expect(mapOverviewTotal({ doc_num: 0 })).toEqual({
      pending: 0,
      ingested: 0,
      pendingFailed: 0,
      processing: 0,
      finished: 0,
      failed: 0,
      cancelled: 0,
      downloaded: 0,
    });
  });

  it('defaults every field to 0 when data is undefined', () => {
    expect(mapOverviewTotal(undefined)).toEqual({
      pending: 0,
      ingested: 0,
      pendingFailed: 0,
      processing: 0,
      finished: 0,
      failed: 0,
      cancelled: 0,
      downloaded: 0,
    });
  });

  it('fills only the counts present in a partial status', () => {
    expect(
      mapOverviewTotal({
        doc_num: 12,
        status: { unstart_count: 3, running_count: 2 },
      }),
    ).toEqual({
      pending: 3,
      ingested: 12,
      pendingFailed: 0,
      processing: 2,
      finished: 0,
      failed: 0,
      cancelled: 0,
      downloaded: 3,
    });
  });

  it('does not treat missing doc_num as download-by-source_type', () => {
    const result = mapOverviewTotal({
      status: { unstart_count: 5, fail_count: 9 },
    });
    expect(result.ingested).toBe(0);
    expect(result.pendingFailed).toBe(0);
    expect(result.failed).toBe(9);
  });
});