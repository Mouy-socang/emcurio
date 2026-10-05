"""Install/update this user's Curio hourly launchd job; no model scheduler."""
import os,plistlib,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
LABEL='com.curio.collect.phase1'

def configuration(root=ROOT):
    return {'Label':LABEL,'ProgramArguments':[str(root/'.venv/bin/python'),'-m','curio.curio_monitor'],
            'WorkingDirectory':str(root),'StartInterval':3600,'RunAtLoad':False,
            'EnvironmentVariables':{'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','PYTHONUNBUFFERED':'1'}}

def install():
    target=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
    target.parent.mkdir(parents=True,exist_ok=True)
    domain=f'gui/{os.getuid()}'
    loaded=subprocess.run(['launchctl','print',domain+'/'+LABEL],capture_output=True).returncode==0
    if loaded:subprocess.run(['launchctl','bootout',domain+'/'+LABEL],check=True)
    with target.open('wb') as f:plistlib.dump(configuration(),f)
    subprocess.run(['launchctl','bootstrap',domain,str(target)],check=True)
    print('每小时采集任务已安装：'+LABEL)
    print('首次运行：launchctl kickstart '+domain+'/'+LABEL)

if __name__=='__main__':install()
