"""启动Claude守护进程"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dong.bridge.daemon import start_daemon
start_daemon()
