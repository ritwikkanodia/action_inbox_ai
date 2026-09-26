"""Behind a dedicated HTTPS ingress; never log URL queries or headers."""
bind = '0.0.0.0:8000'
workers = 2
threads = 4
timeout = 60
accesslog = '-'
access_log_format = '%(m)s %(U)s %(s)s %(L)s'
forwarded_allow_ips = ''
limit_request_line = 8190
limit_request_field_size = 8190
