from contextvars import ContextVar
request_versions=ContextVar('request_versions',default=None)
def track_version(kind,value):
    holder=request_versions.get()
    if holder is not None:holder[kind]=value
