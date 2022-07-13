from setuptools import setup, find_namespace_packages
from babel.messages import frontend as babel

setup(
    name='mfa',
    version='0.1.0',
    author='Michael Fladischer',
    author_email='michael.fladischer@medunigraz.at',
    url='https://github.com/medunigraz/mfa',
    packages=find_namespace_packages(),
    setup_requires=(
        'setuptools_scm'
    ),
    use_scm_version=True,
    include_package_data=True,
    install_requires=(
        'Werkzeug',
        'Babel',
        'redis',
        'duo-client',
        'Jinja2',
    ),
    cmdclass={
        'compile_catalog': babel.compile_catalog,
        'extract_messages': babel.extract_messages,
        'init_catalog': babel.init_catalog,
        'update_catalog': babel.update_catalog,
    },
    message_extractors={
        'mug/mfa': [
            ('templates/**.html', 'jinja2', {'encoding': 'utf-8'}),
        ],
    },
)
