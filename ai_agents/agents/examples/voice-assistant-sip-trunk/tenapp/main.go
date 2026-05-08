package main

import (
	"flag"
	"log"
	"os"

	ten "ten_framework/ten_runtime"
)

type appConfig struct {
	PropertyFilePath string
}

type defaultApp struct {
	ten.DefaultApp

	cfg *appConfig
}

func (p *defaultApp) OnConfigure(tenEnv ten.TenEnv) {
	if len(p.cfg.PropertyFilePath) > 0 {
		if b, err := os.ReadFile(p.cfg.PropertyFilePath); err != nil {
			log.Fatalf("Failed to read property file %s, err %v\n", p.cfg.PropertyFilePath, err)
		} else {
			tenEnv.InitPropertyFromJSONBytes(b)
		}
	}

	tenEnv.OnConfigureDone()
}

func startAppBlocking(cfg *appConfig) {
	appInstance, err := ten.NewApp(&defaultApp{
		cfg: cfg,
	})
	if err != nil {
		log.Fatalf("Failed to create the app, %v\n", err)
	}

	appInstance.Run(true)
	appInstance.Wait()

	ten.EnsureCleanupWhenProcessExit()
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)

	cfg := &appConfig{}
	flag.StringVar(&cfg.PropertyFilePath, "property", "", "The absolute path of property.json")
	flag.Parse()

	startAppBlocking(cfg)
}
