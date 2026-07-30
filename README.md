# SerenityDev Local Agent Interface

This is my extension "SerenityDev".
This is a vibe-coded extension/server made for VS Code and Android Studio (TBD), so I can run llm agents locally. 
the folder "serenitydev-0.0.1" is the original folder. models would go here for registration.
for model details, see serenitydevserver.py and ctrl-f "gguf"

## Features

llama-server model registry
llama.cpp inference with llama-server fallback
hyper-specific model routing (registration of alternate models to be added)

### Supervisor vs Workers

the supervisor routes to a system pictured below. To skip all this, select a worker (W1-4)
(Note: not pictured is W4, Qwen3.6-27B.)

![The general idea behind Supervisor mode.](ModelChart.jpg)

This is simply an extension for Local Agentic purposes, because I'm picky and don't like any available options.
it's not meant to work for many purposes, yet.
this isn't a professional app, I'm still improving and finding out what works and how to do all this, so consider this a learning experiment project.

> Tip: Many popular extensions utilize animations. This is an excellent way to show off your extension! We recommend short, focused animations that are easy to follow.

## Requirements

see imports.

## Extension Settings

This extension contributes the following settings:

* `Serenity:extension status control panel`: Resume, Pause, Stop this extension.
* @Serenity prompt routing in a new extension box (Icon missing, I know)

## Known Issues

Probably a lot.
Blank icon
further response testing is needed, I just got it responding again.
Models reason directly in chat, not separated properly.
Additional testing and tweaking overall is still needed 


## Release Notes

migrated standalone server folder into VS extension.
Various tweaks and fixes, feature and stability upgrades.

### 1.0.0

Initial release

### 1.0.1

- Details TBA -

## Feedback and Improvements

Do what you want... I'm not an expert by any means.

