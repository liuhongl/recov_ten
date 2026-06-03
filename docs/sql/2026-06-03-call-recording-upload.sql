-- recov_ten full-call mixed recording upload.
-- Adds the business link from call_record to the unified sys_oss file record.

alter table public.call_record
add column if not exists recording_oss_id bigint;

comment on column public.call_record.recording_oss_id
is '完整通话混音录音对应的 sys_oss.oss_id';
