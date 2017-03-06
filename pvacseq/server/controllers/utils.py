#common utils for all controllers
import os
from glob import iglob
import json
import sys
import subprocess
import watchdog.events
import postgresql as psql
from postgresql.exceptions import Exception as psqlException
from .watchdir import Observe
import atexit
from postgresql.exceptions import UndefinedTableError

class dataObj(dict):
    def __init__(self, datafiles):
        super().__init__()
        super().__setitem__(
            '_datafiles',
            {datafile:[] for datafile in datafiles}
        )

    def __setitem__(self, key, value):
        if key not in self and key not in {k for parent in self['_datafiles'] for k in parent}:
            raise KeyError("Key %s has no associated file.  Use addKey() first"%key)
        super().__setitem__(key, value)

    def addKey(self, key, value, dest):
        """Adds a new root key to the app data storage object."""
        if dest not in self['_datafiles']:
            self['_datafiles'][dest] = [key]
        else:
            self['_datafiles'][dest].append(key)
        super().__setitem__(key, value)

    def save(self):
        """Saves the data object to the various data files"""
        for datafile in self['_datafiles']:
            os.makedirs(os.path.dirname(datafile), exist_ok=True)
            writer = open(datafile, 'w')
            json.dump(
                {
                    key:self[key] for key in self['_datafiles'][datafile] if key in self
                },
                writer,
                indent='\t'
            )
            writer.close()

_descriptions = {
    'json':"Metadata regarding a specific run of pVAC-Seq",
    'chop.tsv':"Processed and filtered data, with peptide cleavage data added",
    'combined.parsed.tsv':"Processed data from IEDB, but with no filtering or extra data",
    'filtered.binding.tsv':"Processed data filtered by binding strength",
    'filtered.coverage.tsv':"Processed data filtered by binding strength and coverage",
    'stab.tsv':"Processed and filtered data, with peptide stability data added",
    'final.tsv':"Final output data",
    'tsv':"Raw input data parsed out of the input vcf"
}

def descriptions(ext):
    if ext in _descriptions:
        return _descriptions[ext]
    return "Unknown File"

def column_filter(column):
    """standardize column names"""
    return column.replace(' ', '_').replace('-', '_').lower().strip()

def loaddata(datafiles):
    data = dataObj({datafiles[datafile] for datafile in datafiles if not datafile.endswith('-dir')})
    for datafile in data['_datafiles']:
        if os.path.isfile(datafile):
            current = json.load(open(datafile))
            for (key, value) in current.items():
                data.addKey(key, value, datafile)
    return data

def initialize(current_app):
    """Setup anything that needs to be configured before the app start"""
    #This section is run once, when the API spins up
    print("Initializing app configuration")
    #First, read all the json config files to load app configuration
    config = {'storage': {}}
    config_dir = os.path.join(
        os.path.dirname(__file__),
        '..',
        'config'
    )
    user_config_dir = os.path.expanduser("~/.pvacseq")
    if not os.path.isdir(user_config_dir):
        os.makedirs(user_config_dir)
    #For every config file predefined in the config directory,
    #first read and load the file, then
    #check the user config directory for an override
    for configfile in iglob(os.path.join(config_dir, '*.json')):
        reader = open(configfile)
        key = os.path.splitext(os.path.basename(configfile))[0]
        config[key] = json.load(reader)
        reader.close()
        try:
            reader = open(os.path.join(user_config_dir, os.path.basename(configfile)))
            if key == 'schema':
                config[key].update({
                    column_filter(k):v for (k,v) in json.load(reader).items()
                })
            else:
                config[key].update(json.load(reader))
            reader.close()
        except FileNotFoundError:
            pass
    for key in config['files']:
        config['files'][key] = os.path.abspath(os.path.expanduser(config['files'][key]))
    current_app.config.update(config) #save to the app configuration object

    #Now load the data object from the files specified in the configuration
    data = loaddata(current_app.config['files'])
    if 'processid' not in data:
        data.addKey('processid', 0, current_app.config['files']['processes'])
    if 'dropbox' not in data:
        data.addKey('dropbox', {}, current_app.config['files']['dropbox'])
    #Check the last reboot (because pid's won't remain valid after a reboot)
    reboot = subprocess.check_output(['last', 'reboot']).decode().split("\n")[0]
    current_app.config['reboot'] = reboot
    if 'reboot' in data and data['reboot'] != reboot:
        print("A reboot has occurred since the server was first started")
        print(
            "pid's of old pVAC-Seq runs with id's",
            data['processid'],
            "and lower may be innacurate"
        )
    current_app.config['storage']['children']={}
    current_app.config['storage']['manifest']={}

    #Establish a connection to the local postgres database
    try:
        tmp = psql.open("localhost/postgres")
    except psqlException as e:
        raise SystemExit("Unable to connect to your Postgres server.\
                         The pVAC-Seq API requires a running local Postgres server") from e
    if not len(tmp.prepare("SELECT 1 FROM pg_database WHERE datname = $1")('pvacseq')):
        tmp.execute("CREATE DATABASE pvacseq")
    tmp.close()
    db = psql.open("localhost/pvacseq")
    current_app.config['storage']['db'] = db

    #setup directory structure:
    os.makedirs(
        os.path.join(current_app.config['files']['data-dir'],'input'),
        exist_ok=True
    )
    os.makedirs(
        os.path.join(current_app.config['files']['data-dir'],'results'),
        exist_ok=True
    )
    os.makedirs(
        os.path.join(current_app.config['files']['data-dir'],'archive'),
        exist_ok=True
    )
    os.makedirs(
        os.path.join(current_app.config['files']['data-dir'],'.tmp'),
        exist_ok=True
    )

    #Setup the watchers to observe the files
    current_app.config['storage']['watchers'] = []

    dbr = os.path.join(current_app.config['files']['data-dir'],'archive')
    dropbox_watcher = Observe(dbr)
    dropbox_watcher.subscribe(lambda x:print("Dropbox Event:", x))
    #Now we set up event handlers for the dropbox
    #This ensures that file ids are held consistent
    current = {
        os.path.join(path, filename)
        for (path, _, files) in os.walk(dbr)
        for filename in files
    }
    for (key, filename) in data['dropbox'].items():
        if type(data['dropbox'][key])==str:
            print("Updating dropbox entry",key,"to new format")
            data['dropbox'][key] = {
                'fullname':os.path.join(
                    dbr,
                    filename
                ),
                'display_name':os.path.relpath(
                    filename,
                    dbr
                ),
                'description':descriptions(
                    '.'.join(os.path.basename(filename).split('.')[1:])
                )
            }
    recorded = {item['fullname'] for item in data['dropbox'].values()}
    targets = {data['dropbox'][k]['fullname'] for k in data['dropbox'] if data['dropbox'][k]['fullname'] in recorded-current}
    for fileID in targets:
        del data['dropbox'][fileID]
    fileID = 0
    for filename in current-recorded:
        while str(fileID) in data['dropbox']:
            fileID += 1
        print("Assigning file:", fileID,"-->",filename)
        data['dropbox'][str(fileID)] = {
            'fullname':os.path.abspath(os.path.join(
                dbr,
                filename
            )),
            'display_name':os.path.relpath(
                filename,
                dbr
            ),
            'description':descriptions(
                '.'.join(os.path.basename(filename).split('.')[0b1:])
            )
        }

    data_path = current_app.config['files']
    def _create(event):
        data = loaddata(data_path)
        filename = os.path.relpath(
            event.src_path,
            dbr
        )
        fileID = 0
        while str(fileID) in data['dropbox']:
            fileID += 1
        print("Creating file:", fileID, "-->",filename)
        data['dropbox'][str(fileID)] = {
            'fullname':os.path.abspath(os.path.join(
                dbr,
                filename
            )),
            'display_name':filename,
            'description':descriptions(
                '.'.join(os.path.basename(filename).split('.')[0b1:])
            )
        }
        data.save()
    dropbox_watcher.subscribe(
        _create,
        watchdog.events.FileCreatedEvent
    )

    def _delete(event):
        data = loaddata(data_path)
        filename = os.path.relpath(
            event.src_path,
            dbr
        )
        for key in list(data['dropbox']):
            if data['dropbox'][key] == filename:
                del data['dropbox'][key]
                print("Deleting file:",key,'-->', filename)
                query = db.prepare("SELECT 1 FROM information_schema.tables WHERE table_name = $1")
                if len(query('data_dropbox_'+str(key))):
                    db.execute("DROP TABLE data_dropbox_"+str(key))
                data.save()
                return
    dropbox_watcher.subscribe(
        _delete,
        watchdog.events.FileDeletedEvent
    )

    def _move(event):
        data = loaddata(data_path)
        filesrc = os.path.relpath(
            event.src_path,
            dbr
        )
        filedest = os.path.relpath(
            event.dest_path,
            dbr
        )
        for key in data['dropbox']:
            if data['dropbox'][key] == filesrc:
                data['dropbox'][key] = {
                    'fullname':os.path.abspath(os.path.join(
                        dbr,
                        filedest
                    )),
                    'display_name':os.path.relpath(
                        filedest,
                        dbr
                    ),
                    'description':descriptions(
                        '.'.join(os.path.basename(filedest).split('.')[0b1:])
                    )
                }
                print("Moving file:", key,'(',filesrc,'-->',filedest,')')
                data.save()
                return
    dropbox_watcher.subscribe(
        _move,
        watchdog.events.FileMovedEvent
    )
    current_app.config['storage']['watchers'].append(dropbox_watcher)

    resultdir = os.path.join(current_app.config['files']['data-dir'], 'results')
    results_watcher = Observe(resultdir)
    results_watcher.subscribe(lambda x:print("Results Event:", x))
    for processID in range(data['processid']+1):
        processkey = 'process-%d'%processID
        if processkey in data:
            print("Checking files for process", processID)
            if 'files' in data[processkey]:
                if type(data[processkey]['files']) == list:
                    print("Updating file manifest of process",processID,"to new format")
                    data[processkey]['files']={
                        fileID:{
                            'fullname':filename,
                            'display_name':os.path.relpath(
                                filename,
                                data[processkey]['output']
                            ),
                            'description':descriptions(
                                '.'.join(os.path.basename(filename).split('.')[1:])
                            )
                        }
                        for (filename, fileID) in zip(
                            data[processkey]['files'],
                            range(sys.maxsize)
                        )
                    }
            else:
                data[processkey]['files'] = {}
            current = {
                os.path.join(path, filename)
                for (path, _, files) in os.walk(data[processkey]['output'])
                for filename in files
            }
            recorded = {entry['fullname']:k for k,entry in data[processkey]['files'].items()}
            for fileID in recorded.keys()-current:
                print("Deleting file",fileID,"from manifest")
                fileID = recorded[fileID]
                del data[processkey]['files'][fileID]
            for filename in current-recorded.keys():
                fileID = len(data[processkey]['files'])
                while str(fileID) in data[processkey]['files']:
                    fileID += 1
                fileID = str(fileID)
                print("Assigning file:",fileID,"-->",filename)
                data[processkey]['files'][fileID] = {
                    'fullname':filename,
                    'display_name':os.path.relpath(
                        filename,
                        data[processkey]['output']
                    ),
                    'description':descriptions(
                        '.'.join(os.path.basename(filename).split('.')[1:])
                    )
                }

    def _create(event):
        data = loaddata(data_path)
        parentpaths = {
            (data['process-%d'%i]['output'], i)
            for i in range(data['processid']+1)
            if 'process-%d'%i in data
        }
        filepath = event.src_path
        for (parentpath, parentID) in parentpaths:
            if os.path.commonpath([filepath, parentpath])==parentpath:
                print("New output from process",parentID)
                processkey = 'process-%d'%parentID
                fileID = len(data[processkey]['files'])
                while str(fileID) in data[processkey]['files']:
                    fileID+=1
                fileID = str(fileID)
                display_name = os.path.relpath(
                    filepath,
                    data[processkey]['output']
                )
                print("Assigning id",fileID,'-->',display_name)
                data[processkey]['files'][fileID] = {
                    'fullname':filepath,
                    'display_name':display_name,
                    'description':descriptions(
                        '.'.join(os.path.basename(filepath).split('.')[1:])
                    )
                }
                data.save()
                return
    results_watcher.subscribe(
        _create,
        watchdog.events.FileCreatedEvent
    )

    def _delete(event):
        data = loaddata(data_path)
        parentpaths = {
            (data['process-%d'%i]['output'], i)
            for i in range(data['processid']+1)
            if 'process-%d'%i in data
        }
        filepath = event.src_path
        for (parentpath, parentID) in parentpaths:
            if os.path.commonpath([filepath, parentpath])==parentpath:
                print("Deleted output from process",parentID)
                processkey = 'process-%d'%parentID
                for (fileID, filedata) in list(data[processkey]['files'].items()):
                    if filedata['fullname'] == filepath:
                        del data[processkey]['files'][fileID]
                        print("Deleted file:", fileID,'-->',filepath)
                        query = db.prepare("SELECT 1 FROM information_schema.tables WHERE table_name = $1")
                        if len(query('data_%d_%s'%(parentID, fileID))):
                            db.execute("DROP TABLE data_%d_%s"%(parentID, fileID))
                data.save()
                return
    results_watcher.subscribe(
        _delete,
        watchdog.events.FileDeletedEvent
    )

    def _move(event):
        data = loaddata(data_path)
        filesrc = event.src_path
        filedest = event.dest_path
        parentpaths = {
            (data['process-%d'%i]['output'], i)
            for i in range(data['processid']+1)
            if 'process-%d'%i in data
        }
        srckey = ''
        destkey = ''
        for (parentpath, parentID) in parentpaths:
            if os.path.commonpath([filesrc, parentpath])==parentpath:
                srckey = 'process-%d'%parentID
            elif os.path.commonpath([filedest, parentpath]) == parentpath:
                destkey = 'process-%d'%parentID

        if srckey == destkey:
            for (fileID, filedata) in data[srckey]['files'].items():
                if filedata['fullname'] == filesrc:
                    data[srckey]['files'][fileID] = {
                        'fullname':filedest,
                        'display_name':os.path.relpath(
                            filedest,
                            data[srckey]['output']
                        ),
                        'description':descriptions(
                            '.'.join(os.path.basename(filedest).split('.')[1:])
                        )
                    }
        else:
            _delete(event)
            evt = lambda:None
            evt.src_path = event.dest_path
            _create(evt)
    results_watcher.subscribe(
        _move,
        watchdog.events.FileMovedEvent
    )
    current_app.config['storage']['watchers'].append(results_watcher)



    def cleanup():
        print("Cleaning up observers and database connections")
        for watcher in current_app.config['storage']['watchers']:
            watcher.stop()
            watcher.join()
        if 'db-clean' in current_app.config:
            for table in current_app.config['db-clean']:
                try:
                    current_app.config['storage']['db'].execute("DROP TABLE %s"%table)
                except UndefinedTableError:
                    pass
        current_app.config['storage']['db'].close()

    atexit.register(cleanup)
    current_app.config['storage']['loader'] = lambda:loaddata(data_path)
    data.save()

    print("Initialization complete.  Booting API")