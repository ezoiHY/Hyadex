plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.compose.compiler)
  alias(libs.plugins.kotlin.serialization)
}

android {
  namespace = "dev.hyadex.android"
  compileSdk = 37
  defaultConfig {
    applicationId = "dev.hyadex.android"
    minSdk = 29
    targetSdk = 37
    versionCode = 1
    versionName = "0.1.0"
  }

  buildTypes {
    release {
      // R8 drops what the app never calls (most of the icon set, unused library code).
      isMinifyEnabled = true
      isShrinkResources = true
      proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
      // Signed with the local debug key until there is a release key, so the APK installs as is.
      signingConfig = signingConfigs.getByName("debug")
    }
  }
  compileOptions {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
  }
  buildFeatures {
    compose = true
    aidl = false
    buildConfig = false
    shaders = false
  }

  packaging {
    resources {
      excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }
  }
}

composeCompiler {
  // Immutable timeline models: unchanged rows skip recomposition.
  stabilityConfigurationFiles.add(project.layout.projectDirectory.file("compose-stability.conf"))
}

dependencies {
  val composeBom = platform(libs.androidx.compose.bom)
  implementation(composeBom)
  androidTestImplementation(composeBom)

  // Core Android dependencies
  implementation(libs.androidx.core.ktx)
  implementation(libs.androidx.lifecycle.runtime.ktx)
  implementation(libs.androidx.activity.compose)

  // Arch Components
  implementation(libs.androidx.lifecycle.runtime.compose)
  implementation(libs.androidx.lifecycle.viewmodel.compose)

  // Compose
  implementation(libs.androidx.compose.ui)
  implementation(libs.androidx.compose.ui.tooling.preview)
  implementation(libs.androidx.compose.material3)
  // Tooling
  debugImplementation(libs.androidx.compose.ui.tooling)
  // Instrumented tests
  androidTestImplementation(libs.androidx.compose.ui.test.junit4)
  debugImplementation(libs.androidx.compose.ui.test.manifest)

  // Local tests: jUnit, coroutines, Android runner
  testImplementation(libs.junit)
  testImplementation(libs.kotlinx.coroutines.test)

  // Instrumented tests: jUnit rules and runners
  androidTestImplementation(libs.androidx.test.core)
  androidTestImplementation(libs.androidx.test.ext.junit)
  androidTestImplementation(libs.androidx.test.runner)
  androidTestImplementation(libs.androidx.test.espresso.core)

  implementation(libs.androidx.compose.material.icons)

  // Server: REST and event sockets, JSON, images, Markdown
  implementation(libs.okhttp)
  implementation(libs.kotlinx.serialization.json)
  implementation(libs.kotlinx.coroutines.android)
  implementation(libs.coil.compose)
  implementation(libs.coil.network.okhttp)
  implementation(libs.markdown.renderer.m3)
  implementation(libs.markdown.renderer.code)
  // Pairing: scans the code the Mac shows; works without Google Play services.
  implementation(libs.zxing.embedded)
  implementation(libs.zxing.core)
}
