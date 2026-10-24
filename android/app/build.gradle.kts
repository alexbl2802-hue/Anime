plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.animedub.mobile"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.animedub.mobile"
        minSdk = 24
        targetSdk = 34
        // Номер сборки GitHub растёт с каждым запуском — новая версия ставится поверх старой.
        versionCode = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
        versionName = "1.0.$versionCode"
    }

    // Один и тот же ключ для всех сборок, иначе Android не даст обновить приложение.
    // Это ключ для личного приложения, а не для Google Play.
    signingConfigs {
        create("shared") {
            storeFile = file("animedub.keystore")
            storePassword = "animedub"
            keyAlias = "animedub"
            keyPassword = "animedub"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("shared")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    lint {
        checkReleaseBuilds = false
        abortOnError = false
    }
}

dependencies {
    implementation("androidx.media3:media3-exoplayer:1.4.1")
    implementation("androidx.media3:media3-ui:1.4.1")
}
