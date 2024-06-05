from setuptools import setup

requirements = [
    'psycopg2-binary>=2',
    'python-dateutil>=2',
    'PyYAML>=3',
    'newrelic>=7.16.0.178'
]

setup(
    name = "rubydj-pyworker",
    version = '1.1.0',
    description="A pure Python worker for Ruby-based DelayedJobs with Newrelic reporting.",
    author="Rayyan Systems, Inc.",
    author_email="support@rayyan.ai",
    url="https://github.com/rayyansys/pyworker",
    license="MIT",
    packages=['pyworker'],
    install_requires = requirements
)
