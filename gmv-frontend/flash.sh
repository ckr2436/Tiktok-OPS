#!/bin/bash
rm -rf dist/*
rm -rf ../frontend/*
pnpm run build && \
        cp -r dist/* ../frontend
