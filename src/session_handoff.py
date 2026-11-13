"""Local-date/run-organized native handoff; never invent EDF units/calibration."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import csv,json,sqlite3,uuid,shutil,os
from contextlib import closing

def write_json(path,obj):
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(path)

def export_handoff(store):
    provenance=store.locked_config.get('experiment',{}).get('provenance')
    if store.run_mode!='brainco' or not provenance:return
    db=store.database
    row=dict(db.execute('SELECT * FROM sessions WHERE session_id=?',(store.session_id,)).fetchone())
    day=datetime.fromisoformat(row['start_time']).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%y%m%d')
    root=store.dataset_root.parent/('sub-'+store.participant_id.removeprefix('sub-'))
    root.mkdir(parents=True,exist_ok=True)
    # A daily session groups separately recorded runs. Native archive remains
    # authoritative for crash recovery and verified fixed-schedule resume.
    for index in range(1,100):
        session=root/f'ses-{day}{index:02}'
        if not session.exists():
            session.mkdir()
            state=dict(schema_version=2,session_id=str(uuid.uuid4()),participant_id=store.participant_id,paradigm_code=provenance['paradigm_code'],started_at=row['start_time'],runs=[],eegqc_ready=False,format='brainco-native-sqlite',limitations=['SDK units/calibration unverified; no EDF/BDF fabricated','Software estimated alignment; no hardware trigger'])
            write_json(session/'session.json',state)
            break
        state=json.loads((session/'session.json').read_text(encoding='utf-8'))
        if state['paradigm_code']==provenance['paradigm_code']:break
    else:raise RuntimeError('Daily session identifiers exhausted')
    entry=next((r for r in state['runs'] if r['source_session_id']==store.session_id),None)
    if entry and entry.get('handoff_complete'):return session
    if entry is None:
        entry=dict(run=len(state['runs'])+1,recording_id=str(uuid.uuid4()),source_session_id=store.session_id,handoff_complete=False)
        state['runs'].append(entry)
        write_json(session/'session.json',state)
    run=entry['run']
    prefix=f'{root.name}_{session.name}_task-bcscene_run-{run:02}'
    eeg=session/'eeg';behavior=session/'behavior'
    eeg.mkdir(exist_ok=True);behavior.mkdir(exist_ok=True)
    rec=db.execute('SELECT * FROM eeg_recordings WHERE session_id=?',(store.session_id,)).fetchone()
    rate=rec['sampling_rate_hz'] if rec else 250
    signal=None
    if rec:
        source=store.dataset_root/rec['edf_path']
        signal=eeg/(prefix+'_eeg.sqlite3')
        # Consistent, self-contained native database, not a mislabeled EDF.
        with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as original, closing(sqlite3.connect(signal)) as copy:
            original.backup(copy)
    events=[]
    for event in db.execute('SELECT e.*,y.eeg_sample_index,y.sync_quality FROM events e LEFT JOIN eeg_sync y USING(event_id) WHERE e.session_id=? ORDER BY e.event_seq',(store.session_id,)):
        meta=json.loads(event['metadata_json'] or '{}');sample=event['eeg_sample_index']
        events.append(dict(onset=(sample-1)/rate if sample is not None else '',duration=0,event_id=event['event_id'],trial_id=event['trial_id'],event_type=event['event_type'],trigger_code=event['trigger_code'],sample_index_1based=sample,sync_quality=event['sync_quality'],schedule_trial_id=meta.get('schedule_trial_id'),image_id=meta.get('image_id'),scene=meta.get('scene'),view=meta.get('view'),object_id=meta.get('object_id'),software_monotonic_ns=event['timestamp_ns']))
    event_fields=list(events[0]) if events else ['onset','duration','event_id','event_type']
    with (eeg/(prefix+'_events.csv')).open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=event_fields);writer.writeheader();writer.writerows(events)
    with (eeg/(prefix+'_channels.csv')).open('w',encoding='utf-8',newline='') as f:
        writer=csv.writer(f);writer.writerow(['name','type','units'])
        writer.writerows((f'Ch{i}','EEG','sdk_native_unverified') for i in range(1,33))
    trials=[dict(t) for t in db.execute('SELECT * FROM trials WHERE session_id=? ORDER BY global_index',(store.session_id,))]
    with (behavior/(prefix+'_behavior.csv')).open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(trials[0]) if trials else ['trial_id','status']);writer.writeheader();writer.writerows(trials)
    write_json(behavior/(prefix+'_behavior.json'),dict(row_semantics='one planned trial; aborted may mean not presented',response_task=False,selection='Inspect status, validity, invalid_reason and event exclusion metadata'))
    write_json(eeg/(prefix+'_events.json'),dict(onset='seconds from this run first saved EEG sample; blank if not sample-aligned',duration='point event, 0; image duration obtained from onset/offset pair',trigger_codes={'101':'IMAGE_ONSET','102':'IMAGE_OFFSET','200':'TRIGGER_TEST'},timing='software_estimated; no hardware trigger',software_monotonic_ns='host monotonic time, not UTC'))
    write_json(eeg/(prefix+'_eeg.json'),dict(TaskName='bcscene',SamplingFrequency=rate,EEGChannelCount=32,EEGReference='unknown',SoftwareFilters='not inferred',PowerLineFrequency=None,units='sdk_native_unverified',hardware_sync_verified=False,provenance=provenance,source_native_session=str(store.session_dir),recording_status=row['status']))
    entry.update(started_at=row['start_time'],ended_at=row['end_time'],status=row['status'],signal=str(signal.relative_to(session)) if signal else None,source_native_session=str(store.session_dir),handoff_complete=True)
    state['recording_status']=row['status'];state['ended_at']=row['end_time']
    write_json(session/'session.json',state)
    return session
