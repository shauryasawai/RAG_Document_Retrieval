from setuptools import setup, find_packages

setup(
    name='RAG_Document_Retrieval',
    version='1.0.0',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'django',
        
    ],
    entry_points={
        'console_scripts': [
            'manage.py=manage:main',
        ],
    },
)
