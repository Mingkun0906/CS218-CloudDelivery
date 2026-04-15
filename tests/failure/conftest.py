"""Failure test conftest.

Pytest discovers conftest.py files up the directory tree automatically, so all
fixtures defined in tests/unit/conftest.py (fake_redis, dynamodb_table,
aws_mock, sns_topic) are available here without re-declaration.

This file exists solely to make the scope explicit and document the dependency.
"""
